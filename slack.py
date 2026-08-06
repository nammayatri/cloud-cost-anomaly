"""Slack delivery: a readable summary, then per-account tables, then the workbook.

The root message answers only "how much, and is that within budget". Every
breakdown lives in threaded replies, so the channel view stays a few lines
regardless of how many accounts exist.

Tables are rendered as monospace code blocks rather than Block Kit fields. Slack
has no real table primitive, and a column of rupee figures is only comparable when
the digits line up — proportional text turns a cost column into noise.
"""

import logging

from slack_sdk import WebClient

import collect
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


def _movers_blocks(cfg: dict, report: dict, limit: int = 10) -> list[dict]:
    """Biggest day-over-day increases across every account.

    Ranked by absolute money moved, not percent: a 400% jump on a ₹20 service is
    trivia, while a 12% rise on the biggest line item is what's worth chasing.
    """
    movers = []
    for s in report["sections"]:
        for row in s["rows"]:
            delta = row["today_report"] - row["yesterday_report"]
            if delta > 0:
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
    movers.sort(reverse=True, key=lambda m: m["delta"])
    rc = cfg["report_currency"]
    dec = 0 if rc == "INR" else 2
    # Both absolute levels are shown, not just the delta: "+6,537" means something
    # different on a ₹33k line than on a ₹300 one, and the rank order alone does
    # not convey that.
    rows = [[m["service"][:30], m["account"][:16], _num(m["today"], dec),
             _num(m["prev"], dec), "+" + _num(m["delta"], dec), _pct_cell(m["pct"])]
            for m in movers[:limit]]
    return _chunk_code_blocks(
        "*Biggest increases vs the previous day*",
        _mono_table(["Service", "Account", f"cost{rc}", "prev day", "increase", "vs prev"],
                    rows, left_cols=(0, 1)))


def post(cfg: dict, report: dict, xlsx_path: str) -> None:
    """Root message, then the breakdown as threaded replies, then the workbook."""
    client = WebClient(token=cfg["slack_bot_token"])
    d = report["date"]

    top_blocks, attachments = build_root_blocks(cfg, report)
    main = client.chat_postMessage(
        channel=cfg["slack_channel_id"],
        blocks=top_blocks,
        attachments=attachments,
        text=f"Cloud cost report — {d.isoformat()}",
        unfurl_links=False,
        unfurl_media=False,
    )
    channel, ts = main["channel"], main["ts"]

    def reply(blocks, fallback):
        if blocks:
            client.chat_postMessage(channel=channel, thread_ts=ts, text=fallback,
                                    blocks=blocks, unfurl_links=False, unfurl_media=False)

    infra = {s["cloud"] for s in report["sections"] if s["cloud"] not in ("GMP", "HV")}

    # 1 — account split of the infrastructure clouds
    reply(_account_split_blocks(cfg, report, infra, "*Cloud — account split*"),
          "Cloud account split")

    # 1b — third-party vendor: one line item, split by module (per-service table)
    vend = next((sec for sec in report["sections"] if sec["cloud"] == "VENDOR"), None)
    if vend:
        reply(_section_table_blocks(cfg, report, vend), "Vendor by module")

    # 2 — Maps: project split plus the per-API request volumes
    maps_blocks = _account_split_blocks(cfg, report, {"GMP"}, "*Maps — account split*")
    maps_blocks += _gmp_table_blocks(cfg, report)
    reply(maps_blocks, "Maps breakdown")

    # 3 — monthly run-rate against budget
    reply(_runrate_blocks(cfg, report), "Monthly run-rate")

    # 4 — unit economics
    reply(_per_ride_blocks(cfg, report), "Cost per ride")

    # Detail: per-service tables, then the biggest movers.
    for section in sorted(report["sections"], key=lambda x: x["total_report"], reverse=True):
        if section["cloud"] in ("GMP", "VENDOR"):
            continue          # GMP via the per-API table, vendor via the by-module reply
        reply(_section_table_blocks(cfg, report, section), f"{section['label']} breakdown")
    reply(_movers_blocks(cfg, report), "Biggest increases")

    # The workbook is the deliverable — if the upload fails the run has not really
    # succeeded, so this is allowed to raise rather than being swallowed.
    client.files_upload_v2(
        channel=channel,
        thread_ts=ts,
        file=xlsx_path,
        filename=f"{d.isoformat()}-cloud-costs.xlsx",
        title=f"Cloud costs {d.isoformat()}",
        initial_comment="Full per-service breakdown, 7 days per account.",
    )
    log.info("Posted report and workbook to %s", cfg["slack_channel_id"])
