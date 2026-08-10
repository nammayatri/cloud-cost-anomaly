"""Slack delivery: a readable summary, then per-account tables, then the workbook.

The root message answers only "how much, and is that within budget". Every
breakdown lives in threaded replies, so the channel view stays a few lines
regardless of how many accounts exist.

Tables are rendered as monospace code blocks rather than Block Kit fields. Slack
has no real table primitive, and a column of rupee figures is only comparable when
the digits line up — proportional text turns a cost column into noise.
"""

import logging
import re
import tempfile
import time
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import collect
import image_render
import money

log = logging.getLogger("cost-anomaly.slack")

# Slack rejects a section text block over 3000 chars. Leave margin for the fence
# and title.
_MAX_BLOCK = 2800


def _mention_one(m: str) -> str:
    m = m.strip().lstrip("@")
    if not m:
        return ""
    if m in ("here", "channel", "everyone"):
        return f"<!{m}>"
    if m.startswith(("U", "W")):
        return f"<@{m}>"
    if m.startswith("S"):
        return f"<!subteam^{m}>"
    # Anything else passes through as literal text. Slack only NOTIFIES on an ID —
    # a bare "@name" renders as grey text and pings nobody — so a plain name here
    # is a display label, not an alert.
    return m


def _mention_text(mention: str) -> str:
    """Slack mention syntax for one or more comma-separated targets.

    Accepts 'here', 'channel', user IDs (U…/W…) and usergroup IDs (S…), e.g.
    "here,U01234567,U08234567". Handling the list here rather than at the call
    site means a multi-target MENTION cannot silently degrade into literal text.
    """
    if not mention:
        return ""
    return " ".join(filter(None, (_mention_one(m) for m in mention.split(","))))


def _num(v: float, decimals: int = 2) -> str:
    return f"{v:,.{decimals}f}"


def _pct_cell(pct) -> str:
    if pct is None:
        return "-"
    if pct == float("inf"):
        return "new"
    return f"{pct:+.0f}%"


def _amount_pct(amount: float, pct, decimals: int = 0) -> str:
    """Baseline amount with its percentage change in brackets, e.g. '17,628 (+6%)'.

    Kept in one cell rather than two columns because the pair is only meaningful
    together: the percentage says how much it moved, the amount says whether that
    movement is worth anyone's attention.
    """
    return f"{_num(amount, decimals)} ({_pct_cell(pct)})"


def _mono_table(headers: list[str], rows: list[list[str]], left_cols=(0,)) -> str:
    """Fixed-width table. Text columns left-aligned, numeric columns right-aligned.

    Right-aligning the numbers is the entire point: it stacks the digits into a
    column so magnitudes compare by eye, which is how a cost table actually gets
    read.
    """
    if not rows:
        return ""
    widths = [
        max(len(headers[i]), max(len(r[i]) for r in rows))
        for i in range(len(headers))
    ]

    def fmt(cells):
        out = []
        for i, c in enumerate(cells):
            out.append(c.ljust(widths[i]) if i in left_cols else c.rjust(widths[i]))
        return "  ".join(out).rstrip()

    return "\n".join([fmt(headers)] + [fmt(r) for r in rows])


def _chunk_code_blocks(title: str, table: str) -> list[dict]:
    """Split an over-long table across several blocks, repeating the header row and
    keeping the fence valid in each. Slack rejects the whole message otherwise."""
    if not table:
        return []
    lines = table.split("\n")
    header, body = lines[0], lines[1:]
    blocks: list[dict] = []
    current: list[str] = []
    first = True

    def flush():
        nonlocal current, first
        if not current:
            return
        prefix = f"{title}\n" if first else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": prefix + "```\n" + header + "\n" + "\n".join(current) + "\n```"},
        })
        current = []
        first = False

    for line in body:
        if sum(len(x) + 1 for x in current) + len(line) > _MAX_BLOCK:
            flush()
        current.append(line)
    flush()
    return blocks


_CLOUD_NAMES = {"GMP": "Maps"}


def _cloud_name(cloud: str) -> str:
    return _CLOUD_NAMES.get(cloud, cloud)


def _display(section: dict, report: dict) -> str:
    """Human label for a scope. Maps sections drop the project suffix when there is
    only one — the billing project name is an implementation detail, 'Maps' is the
    name everyone uses."""
    if section["cloud"] == "GMP":
        others = [s for s in report["sections"] if s["cloud"] == "GMP"]
        return "Maps" if len(others) == 1 else section["label"].replace("GMP ", "Maps ")
    return section["label"]


def _cloud_order(report: dict) -> list[str]:
    """Clouds ordered by spend, largest first, preserving first-seen order on ties."""
    totals: dict[str, float] = {}
    for s in report["sections"]:
        totals[s["cloud"]] = totals.get(s["cloud"], 0.0) + s["total_report"]
    return [c for c, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)]


def build_root_blocks(cfg: dict, report: dict) -> tuple[list[dict], list[dict]]:
    """Thread root: one line per cloud family, its spend, and its monthly budget.

    The root answers only "how much, and is that within budget". Every breakdown
    lives in the thread, so the channel view stays four lines regardless of how
    many accounts exist.
    """
    d = report["date"]
    t = report["totals"]
    budgets = cfg.get("monthly_budgets") or {}
    days = int(cfg.get("projection_days") or 30)

    mention = _mention_text(cfg.get("mention", ""))
    top_blocks: list[dict] = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"Cloud Costs On {d.isoformat()}", "emoji": True},
    }]

    # "Cloud" = the infrastructure clouds (AWS + GCP). Maps and the third-party
    # vendor are their own named buckets, each shown as a single total line, and
    # all three roll into the grand total. The vendor's display name is config.
    _NAMED = {"GMP", "VENDOR"}
    vendor_label = cfg.get("vendor_cost_head") or "Vendor"
    cloud_total = sum(v for c, v in t["by_cloud"].items() if c not in _NAMED)
    maps_total = t["by_cloud"].get("GMP", 0.0)
    vendor_total = t["by_cloud"].get("VENDOR", 0.0)
    cloud_budget = sum(v for c, v in budgets.items() if c not in _NAMED and v)

    entries = []
    for cloud in _cloud_order(report):
        if cloud in _NAMED:
            continue
        entries.append((f"{_cloud_name(cloud)} All Accounts", t["by_cloud"][cloud],
                        budgets.get(cloud)))
    if maps_total:
        entries.append(("Maps Total", maps_total, (budgets.get("GMP") or None)))
    if vendor_total:
        entries.append((f"{vendor_label} Total", vendor_total, (budgets.get("VENDOR") or None)))
    grand = cloud_total + maps_total + vendor_total
    entries.append(("Total", grand,
                    (cloud_budget + (budgets.get("GMP") or 0) + (budgets.get("VENDOR") or 0)) or None))

    # The goal is shown as a DAILY figure (monthly budget / projection days) so it
    # sits in the same units as the cost beside it. Comparing a day's spend to a
    # monthly budget in the same row invites a 30x misreading.
    rows = []
    for label, val, budget in entries:
        daily_goal = (budget / days) if budget else None
        pct = ((val - daily_goal) / daily_goal * 100.0) if daily_goal else None
        diff = (val - daily_goal) if daily_goal else None
        rows.append([
            label,
            money.fmt(cfg, val),
            money.fmt(cfg, daily_goal) if daily_goal else "-",
            # Rupees first, percent in brackets — a percentage alone cannot say
            # whether the gap is worth chasing.
            (("+" if diff > 0 else "") + money.fmt(cfg, diff) + f" ({pct:+.0f}%)")
            if daily_goal else "-",
        ])
    # A one-line verdict above the table. Emoji live outside the code block on
    # purpose — inside it they are double-width and shear the column alignment.
    total_budget = sum(v for v in budgets.values() if v)
    body = []
    if total_budget:
        projected = t["grand_total"] * days
        over = projected - total_budget
        pct = over / total_budget * 100.0
        emoji = ":red_circle:" if pct >= 10 else (":large_yellow_circle:" if pct > 0
                                                  else ":large_green_circle:")
        verdict = (f"{emoji}  *{abs(pct):.0f}% {'over' if over > 0 else 'under'} budget* — "
                   f"{money.fmt(cfg, abs(over))}/month {'above' if over > 0 else 'below'} plan")
        body.append({"type": "section", "text": {"type": "mrkdwn", "text": verdict}})
    body.append({"type": "section", "text": {"type": "mrkdwn",
        "text": "```\n" + _mono_table(["Account", "Current", "Goal/day", "Rate"], rows)
                + "\n```"}})

    # Mentions last: the numbers are what the reader came for, and a row of pings
    # above them is just a wall to scroll past. Position has no effect on whether
    # Slack notifies — an ID pings from anywhere in the message.
    if mention:
        body.append({"type": "section", "text": {"type": "mrkdwn", "text": mention}})
    return top_blocks, [{"color": _root_color(cfg, report), "blocks": body}]


def _root_color(cfg: dict, report: dict) -> str:
    """Red when the overall run-rate is over budget, grey when there is no budget."""
    budgets = cfg.get("monthly_budgets") or {}
    total_budget = sum(v for v in budgets.values() if v)
    if not total_budget:
        return "#7f8c8d"
    days = int(cfg.get("projection_days") or 30)
    projected = report["totals"]["grand_total"] * days
    pct = (projected - total_budget) / total_budget * 100.0
    return "#d62728" if pct >= 10 else ("#f1c40f" if pct > 0 else "#2ca02c")


def _account_split_blocks(cfg: dict, report: dict, clouds, title: str) -> list[dict]:
    """Per-account totals for the given clouds, with a total row."""
    # Largest spender first — the order someone scanning for "where does the money
    # go" wants. The TOTAL row stays pinned at the bottom.
    secs = sorted([s for s in report["sections"] if s["cloud"] in clouds],
                  key=lambda x: x["total_report"], reverse=True)
    if not secs:
        return []
    rc = cfg["report_currency"]
    dec = 0 if rc == "INR" else 2
    rows = [[_display(s, report), _num(s["total_report"], dec),
             _amount_pct(s["prev_total_report"], s["dod_pct"], dec),
             _amount_pct(s["lw_total_report"], s["wow_pct"], dec)] for s in secs]
    total = sum(s["total_report"] for s in secs)
    prev = sum(s["prev_total_report"] for s in secs)
    lw = sum(s["lw_total_report"] for s in secs)
    rows.append(["TOTAL", _num(total, dec),
                 _amount_pct(prev, _pct_of(total, prev), dec),
                 _amount_pct(lw, _pct_of(total, lw), dec)])
    return _chunk_code_blocks(
        title, _mono_table(["Account", f"cost{rc}", "vs prev day", "vs last week"], rows))


def _pct_of(curr, base):
    if base <= 0:
        return float("inf") if curr > 0 else 0.0
    return (curr - base) / base * 100.0


def _runrate_blocks(cfg: dict, report: dict) -> list[dict]:
    proj = collect.monthly_projection(cfg, report)
    if not proj:
        return []
    days = int(cfg.get("projection_days") or 30)
    rows = [[e["label"], money.fmt(cfg, e["projected"]),
             money.fmt(cfg, e["budget"]) if e["budget"] else "-",
             # Signed amount as well as percent: "+136%" on a small budget and
             # "+10%" on a large one can be the same rupees, and the rupees are
             # what actually has to be found.
             (("+" if e["diff"] > 0 else "") + money.fmt(cfg, e["diff"]))
             if e.get("diff") is not None else "-",
             f"{e['pct']:+.0f}%" if e["pct"] is not None else "-",
             ("OVER" if e["pct"] > 0 else "under") if e["pct"] is not None else ""]
            for e in proj]
    return _chunk_code_blocks(
        f"*Monthly run-rate* _(today x {days})_",
        _mono_table(["Bucket", "Projected", "Budget", "Over/under", "vs Budget", ""], rows))


def _per_ride_blocks(cfg: dict, report: dict) -> list[dict]:
    econ = collect.unit_economics(cfg, report)
    if not econ:
        return []
    rows = [[e["label"], money.fmt(cfg, e["cost"]), f"{e['rides']:,}",
             money.fmt(cfg, e["per_ride"], decimals=2)] for e in econ]
    rides = report["rides"]
    src = "  ·  ".join(f"{k}: {v:,}" for k, v in (rides.get("by_source") or {}).items())
    blocks = _chunk_code_blocks("*Cost per ride*",
                                _mono_table(["Basis", "Cost", "Rides", "Per ride"], rows))
    if src:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": src}]})
    return blocks


def _section_table_blocks(cfg: dict, report: dict, section: dict, limit: int = 20) -> list[dict]:
    """Per-service table for one account.

    Each baseline is shown as an AMOUNT next to its percentage. A percentage alone
    is unreadable without the number behind it — "+217%" could be ₹6 to ₹18 or
    ₹6,000 to ₹18,000, and those warrant completely different reactions.
    """
    rc = cfg["report_currency"]
    dec = 0 if rc == "INR" else 2
    rows = [
        [row["service"][:34],
         _num(row["today_report"], dec),
         _amount_pct(row["yesterday_report"], row["dod_pct"], dec),
         _amount_pct(row["last_week_report"], row["wow_pct"], dec)]
        for row in section["rows"][:limit]
    ]
    if not rows:
        return []

    title = f"*{_display(section, report)}* — {money.fmt(cfg, section['total_report'])}"
    if len(rows) < len(section["rows"]):
        # Never let a cap read as full coverage.
        title += f"  _(top {len(rows)} of {len(section['rows'])}; full list in the workbook)_"
    return _chunk_code_blocks(
        title,
        _mono_table(["Service", f"cost{rc}", "vs prev day", "vs last week"], rows))


def _gmp_table_blocks(cfg: dict, report: dict, limit: int = 20) -> list[dict]:
    """Google Maps per-API table: requests alongside cost, as the old report had it.

    Requests carry the signal here — several Maps APIs sit in a free tier and bill
    at zero, so a cost-only view renders a million calls as a blank line.
    """
    df = report.get("gmp_apis")
    if df is None or df.empty:
        return []
    rc = cfg["report_currency"]
    dec = 0 if rc == "INR" else 2
    shown = df.head(limit)
    rows = [
        [str(r["service"])[:34], _num(float(r["requests"] or 0), 0), _num(float(r["cost"] or 0), dec)]
        for _, r in shown.iterrows()
    ]
    if not rows:
        return []
    # Total across ALL APIs, not just the rows shown — a truncated total would
    # understate Maps and disagree with the root message.
    rows.append(["TOTAL",
                 _num(float(df["requests"].fillna(0).sum()), 0),
                 _num(float(df["cost"].fillna(0).sum()), dec)])
    title = "*Google Maps Platform*"
    if len(shown) < len(df):
        title += f"  _(top {len(shown)} of {len(df)}; TOTAL covers all)_"
    return _chunk_code_blocks(title, _mono_table(["service", "requests", f"current{rc}"], rows))


def _movers_blocks(cfg: dict, report: dict, limit: int = 10, rising: bool = True) -> list[dict]:
    """Biggest day-over-day movers across every account, in one direction.

    Ranked by absolute money moved, not percent: a 400% jump on a ₹20 service is
    trivia, while a 12% rise on the biggest line item is what's worth chasing.
    `rising` picks increases (default) or decreases — the same table, mirrored.
    """
    movers = []
    for s in report["sections"]:
        for row in s["rows"]:
            delta = row["today_report"] - row["yesterday_report"]
            if (delta > 0) if rising else (delta < 0):
                movers.append({
                    "delta": delta,
                    "account": _display(s, report),
                    "service": row["service"],
                    "today": row["today_report"],
                    "prev": row["yesterday_report"],
                    "pct": row["dod_pct"],
                })
    if not movers:
        return []
    # Increases: largest positive first. Decreases: largest drop (most negative) first.
    movers.sort(key=lambda m: m["delta"], reverse=rising)
    rc = cfg["report_currency"]
    dec = 0 if rc == "INR" else 2
    verb = "increase" if rising else "decrease"
    # Both absolute levels are shown, not just the delta: "+6,537" means something
    # different on a ₹33k line than on a ₹300 one, and the rank order alone does
    # not convey that. The delta keeps its sign so the direction is unambiguous.
    rows = [[m["service"][:30], m["account"][:16], _num(m["today"], dec),
             _num(m["prev"], dec), ("+" if m["delta"] > 0 else "") + _num(m["delta"], dec),
             _pct_cell(m["pct"])]
            for m in movers[:limit]]
    title = f"*Biggest {verb}s vs the previous day*"
    return _chunk_code_blocks(
        title,
        _mono_table(["Service", "Account", f"cost{rc}", "prev day", verb, "vs prev"],
                    rows, left_cols=(0, 1)))


# --- Image rendering ----------------------------------------------------------
#
# The same tables the text path builds are rendered to PNGs and posted as images,
# so the morning report reads cleanly on a phone (a Slack code block wraps and
# shrinks on a small screen). The block-builders above stay the single source of
# the numbers — Xyne still renders them as text — and this layer only re-styles
# their output, so the two channels cannot drift.

# Verdict dot colours, matched to the run-rate status (over / slightly over / under).
_VERDICT_COLORS = {
    ":red_circle:": "#d62728",
    ":rotating_light:": "#d62728",
    ":large_yellow_circle:": "#d9a400",
    ":warning:": "#d9a400",
    ":large_green_circle:": "#2ca02c",
}
_INK = "#1d1d21"
_TITLE = "#111318"
_MUTED = "#6b6f76"
_GREEN = "#1a9850"     # cost down — good
_RED = "#d62728"       # cost up — bad

# A signed percentage token, e.g. +90%, -57%, +7% — always signed in these tables.
_PCT_RE = re.compile(r"[+-]\d[\d,]*%")


def _pct_segments(text: str) -> list[tuple[str, str | None]] | None:
    """Split a table row so each signed percentage is its own coloured segment.
    These are cost changes, so the sign reads by impact: a rise (+) is red, a fall
    (-) is green. Everything else keeps the base colour. Returns None when the row
    has no percentage, so it renders as a plain line."""
    segs: list[tuple[str, str | None]] = []
    last = 0
    for m in _PCT_RE.finditer(text):
        if m.start() > last:
            segs.append((text[last:m.start()], None))
        tok = m.group()
        segs.append((tok, _RED if tok[0] == "+" else _GREEN))
        last = m.end()
    if not segs:
        return None
    if last < len(text):
        segs.append((text[last:], None))
    return segs


def _process_text(raw: str) -> tuple[str, str | None, bool]:
    """A markdown/emoji title or verdict line -> (clean text, colour, bold)."""
    color = None
    bold = raw.lstrip().startswith("*")
    for code, c in _VERDICT_COLORS.items():
        if code in raw:
            color = c
            raw = raw.replace(code, "●")      # ● in place of the :emoji:
    raw = raw.replace("*", "").replace("_", "").strip()
    return raw, (color or (_TITLE if bold else _INK)), bold


def _blocks_to_lines(blocks: list[dict]) -> list[dict]:
    """Flatten Block Kit into styled monospace lines for image_render.

    Code-fenced tables render in a regular weight with the header row bolded;
    titles/verdicts render bold/coloured; context blocks render small and muted.
    A header repeated across chunked blocks (the text path splits long tables) is
    emitted once.
    """
    lines: list[dict] = []
    run_header: str | None = None      # header of the current fenced run, to de-dupe
    for b in blocks or []:
        typ = b.get("type")
        if typ == "divider":
            continue
        if typ == "header":
            lines.append({"text": b.get("text", {}).get("text", ""), "size": 17,
                          "bold": True, "color": _TITLE})
            run_header = None
            continue
        if typ == "context":
            txt = " ".join(e.get("text", "") for e in b.get("elements", []))
            txt = txt.replace("*", "").replace("_", "").strip()
            if txt:
                lines.append({"text": txt, "size": 11, "bold": False, "color": _MUTED})
            run_header = None
            continue
        if typ != "section":
            continue
        text = b.get("text", {}).get("text", "")
        for i, part in enumerate(text.split("```")):
            if i % 2 == 1:                          # inside a fence => table rows
                rows = [r for r in part.split("\n") if r.strip()]
                if rows and run_header is not None and rows[0] == run_header:
                    rows = rows[1:]                 # drop a repeated header
                elif rows:
                    run_header = rows[0]
                for r in rows:
                    r = r.rstrip()
                    line = {"text": r, "size": 13, "bold": r == run_header, "color": _INK}
                    if r != run_header:
                        segs = _pct_segments(r)
                        if segs:
                            line["segments"] = segs
                    lines.append(line)
            else:                                   # surrounding title/verdict text
                for ln in part.split("\n"):
                    if not ln.strip():
                        continue
                    t, color, bold = _process_text(ln)
                    lines.append({"text": t, "size": 14, "bold": bold, "color": color})
                    run_header = None
    return lines


def _account(label: str) -> str:
    """Filesystem-safe short name for an image filename."""
    keep = "".join(c if c.isalnum() else "-" for c in label).strip("-").lower()
    return keep or "table"


def _image_specs(cfg: dict, report: dict) -> list[tuple[str, list[dict]]]:
    """Ordered (name, blocks) for every report table, the summary card first.

    Single source of the report's shape, shared by both the Slack and Xyne image
    paths so the two channels render exactly the same set in the same order.
    """
    top_blocks, attachments = build_root_blocks(cfg, report)
    mention = _mention_text(cfg.get("mention", ""))
    # Summary card = header + verdict + totals table, minus the mention row — an
    # image can't ping anyone, so the mentions ride the message text instead.
    summary = top_blocks + [b for b in attachments[0]["blocks"]
                            if b.get("text", {}).get("text") != mention]

    infra = {s["cloud"] for s in report["sections"] if s["cloud"] not in ("GMP", "VENDOR")}
    specs: list[tuple[str, list[dict]]] = [
        ("summary", summary),
        ("cloud-account-split",
         _account_split_blocks(cfg, report, infra, "*Cloud — account split*")),
    ]
    vend = next((sec for sec in report["sections"] if sec["cloud"] == "VENDOR"), None)
    if vend:
        specs.append(("vendor-by-module", _section_table_blocks(cfg, report, vend)))
    specs.append(("maps",
                  _account_split_blocks(cfg, report, {"GMP"}, "*Maps — account split*")
                  + _gmp_table_blocks(cfg, report)))
    specs.append(("monthly-run-rate", _runrate_blocks(cfg, report)))
    specs.append(("cost-per-ride", _per_ride_blocks(cfg, report)))
    for section in sorted(report["sections"], key=lambda x: x["total_report"], reverse=True):
        if section["cloud"] in ("GMP", "VENDOR"):
            continue          # GMP via the per-API table, vendor via the by-module reply
        specs.append((_account(section["label"]),
                      _section_table_blocks(cfg, report, section)))
    specs.append(("biggest-increases", _movers_blocks(cfg, report, rising=True)))
    specs.append(("biggest-decreases", _movers_blocks(cfg, report, rising=False)))
    return [(name, blocks) for name, blocks in specs if blocks]


def render_images(cfg: dict, report: dict, tmpdir: str) -> list[tuple[str, str]]:
    """Render every report table to a PNG. Returns [(name, path)], summary first."""
    out: list[tuple[str, str]] = []
    for i, (name, blocks) in enumerate(_image_specs(cfg, report)):
        lines = _blocks_to_lines(blocks)
        if not lines:
            continue
        path = image_render.render(lines, str(Path(tmpdir) / f"{i:02d}-{name}.png"))
        out.append((name, path))
    return out


def _upload_id(client: WebClient, path: str, title: str, filename: str) -> str:
    """Upload a file WITHOUT sharing it to a channel and return its file id, to be
    referenced from an image block. A raw channel upload can't be threaded under
    (Slack returns empty `shares`); an image block in a real message can."""
    resp = client.files_upload_v2(file=path, filename=filename, title=title)
    f = resp.get("file") or (resp.get("files") or [{}])[0]
    return f["id"]


def _image_block(file_id: str, alt: str) -> dict:
    return {"type": "image", "slack_file": {"id": file_id}, "alt_text": alt}


def _post_blocks(client: WebClient, *, retries: int = 6, **kwargs):
    """chat.postMessage with a retry for `invalid_blocks`. A file referenced by an
    image block is not usable for a second or two after upload; Slack rejects the
    block until it is, so back off and retry rather than dropping the message."""
    for i in range(retries):
        try:
            return client.chat_postMessage(**kwargs)
        except SlackApiError as e:
            if e.response.get("error") == "invalid_blocks" and i < retries - 1:
                time.sleep(1.5)
                continue
            raise


def post(cfg: dict, report: dict, xlsx_path: str) -> None:
    """The summary card + mentions ARE the root message; every breakdown threads
    under it, then the workbook. Each image posts as an inline image block in a real
    message — that yields a reliable message ts to thread under (a raw file upload
    does not). Works with a channel #name or id."""
    client = WebClient(token=cfg["slack_bot_token"])
    d = report["date"]
    channel = cfg["slack_channel_id"]
    tmp = tempfile.mkdtemp(prefix="cost-img-")
    images = render_images(cfg, report, tmp)          # summary first
    mention = _mention_text(cfg.get("mention", ""))

    # Upload every image up front to get file ids. Doing them all first lets the
    # files settle before they're referenced in a block (Slack rejects a just-
    # uploaded file with `invalid_blocks`); _post_blocks retries for the rest.
    uploaded = [(name, _upload_id(client, path, name, f"{name}.png"))
                for name, path in images]

    # Root: headline + mentions, then the summary card as an inline image block.
    head = f"*Cloud costs — {d.isoformat()}*" + (f"\n{mention}" if mention else "")
    _, summary_id = uploaded[0]
    root = _post_blocks(
        client, channel=channel, text=f"Cloud cost report — {d.isoformat()}",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": head}},
                _image_block(summary_id, "cost summary")],
        unfurl_links=False, unfurl_media=False)
    channel, ts = root["channel"], root["ts"]

    # Breakdowns thread under the summary, each as its own image block.
    for name, fid in uploaded[1:]:
        _post_blocks(client, channel=channel, thread_ts=ts, text=name,
                     blocks=[_image_block(fid, name)],
                     unfurl_links=False, unfurl_media=False)

    # The workbook is the deliverable — allowed to raise rather than be swallowed.
    client.files_upload_v2(
        channel=channel, thread_ts=ts, file=xlsx_path,
        filename=f"{d.isoformat()}-cloud-costs.xlsx",
        title=f"Cloud costs {d.isoformat()}",
        initial_comment="Full per-service breakdown, 7 days per account.")
    log.info("Posted report and workbook to %s", cfg["slack_channel_id"])
