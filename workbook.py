"""XLSX report builder — one workbook, one tab per account/project, plus a summary.

Layout mirrors how the numbers are actually read: the Summary tab answers "what
did we spend and what did it cost us per ride", and every other tab answers "which
service moved" for one account. Each detail tab is self-contained so it can be
copied into a thread or a doc on its own.

Costs on detail tabs are shown in BOTH the account's native currency and the
reporting currency. Dropping the native figure would make an AWS tab impossible
to reconcile against the AWS console, which is where anyone who doubts the number
will go next.
"""

from datetime import date, timedelta

import pandas as pd
import xlsxwriter

import collect
import money

# Excel forbids : \ / ? * [ ] in sheet names and caps them at 31 chars.
_ILLEGAL = set(r":\/?*[]")


def _safe_sheet_name(name: str, taken: set[str]) -> str:
    clean = "".join("-" if c in _ILLEGAL else c for c in name)[:31].strip() or "Sheet"
    if clean not in taken:
        taken.add(clean)
        return clean
    # Suffix collisions rather than letting xlsxwriter raise — two projects whose
    # names only differ past character 31 is unlikely but not impossible.
    for i in range(2, 100):
        suffix = f" ({i})"
        candidate = clean[: 31 - len(suffix)] + suffix
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise RuntimeError(f"Could not find a unique sheet name for {name!r}")


class _Widths:
    """Track the widest rendered value per column so columns can be sized to fit.

    Excel does not auto-fit on open — an unset column is 8.43 characters wide, so
    anything longer is silently truncated to "####" or spills over its neighbour.
    Sizing from the real content is the only way the sheet is readable without the
    reader dragging column edges.
    """

    def __init__(self, minimum=9, maximum=52):
        self.w: dict[int, int] = {}
        self.min, self.max = minimum, maximum

    def see(self, col: int, text) -> None:
        n = len(str(text)) if text is not None else 0
        if n > self.w.get(col, 0):
            self.w[col] = n

    def apply(self, ws, padding=3) -> None:
        for col, n in self.w.items():
            ws.set_column(col, col, max(self.min, min(self.max, n + padding)))


class _Styles:
    def __init__(self, wb):
        self.title = wb.add_format({"bold": True, "font_size": 15})
        self.subtitle = wb.add_format({"font_size": 10, "font_color": "#666666"})
        self.header = wb.add_format({
            "bold": True, "bg_color": "#34495E", "font_color": "white",
            "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        self.label = wb.add_format({"bold": True})
        self.text = wb.add_format({"valign": "top"})
        self.wrap = wb.add_format({"text_wrap": True, "valign": "top"})
        self.total = wb.add_format({"bold": True, "top": 2})
        self.big = wb.add_format({"bold": True, "font_size": 13})

    def money(self, wb, code, decimals=None, **extra):
        sym = money.symbol(code)
        if decimals is None:
            decimals = "" if code == "INR" else ".00"
        else:
            decimals = "." + "0" * decimals if decimals else ""
        fmt = {"num_format": f'{sym}#,##0{decimals};[Red]-{sym}#,##0{decimals}'}
        fmt.update(extra)
        return wb.add_format(fmt)


def _money_formats(wb, styles, code):
    return {
        "plain": styles.money(wb, code),
        "total": styles.money(wb, code, bold=True, top=2),
    }


def _pct_fmt(wb, **extra):
    f = {"num_format": '0.0%;[Red]-0.0%'}
    f.update(extra)
    return wb.add_format(f)


def build(path: str, cfg: dict, report: dict) -> str:
    """Write the workbook to `path` and return the path.

    `report` is the structure assembled by main.collect():
      {
        "date": date,
        "sections": [ {label, cloud, currency, rows[], total_native, total_report,
                       prev_total_report, lw_total_report}, ... ],
        "gmp_apis": DataFrame(service, requests, cost) | None,
        "rides": int | None,
        "totals": {...},
      }
    """
    wb = xlsxwriter.Workbook(path, {"constant_memory": False, "default_date_format": "yyyy-mm-dd"})
    styles = _Styles(wb)
    taken: set[str] = set()

    _summary_sheet(wb, styles, taken, cfg, report)
    for section in report["sections"]:
        _detail_sheet(wb, styles, taken, cfg, report, section)
    if report.get("gmp_apis") is not None and not report["gmp_apis"].empty:
        _gmp_sheet(wb, styles, taken, cfg, report)

    wb.close()
    return path


def _summary_sheet(wb, styles, taken, cfg, report):
    ws = wb.add_worksheet(_safe_sheet_name("Summary", taken))
    ws.hide_gridlines(2)
    W = _Widths()

    rc = cfg["report_currency"]
    mfmt = _money_formats(wb, styles, rc)
    pct = _pct_fmt(wb)
    rdec = 0 if rc == "INR" else 2
    cmp_fmt = wb.add_format({"align": "right"})
    d = report["date"]
    t = report["totals"]

    ws.write(0, 0, f"Cloud Costs — {d.strftime('%a %d %b %Y')}", styles.title)
    ws.write(1, 0, f"All amounts in {rc}. AWS converted at {cfg['usd_inr_rate']:.4f} INR/USD "
             f"({cfg.get('fx_source', 'pinned')}).", styles.subtitle)

    r = 3
    budgets = cfg.get("monthly_budgets") or {}
    days = int(cfg.get("projection_days") or 30)
    _NAMED = {"GMP", "VENDOR"}
    cloud_total = sum(v for c, v in t["by_cloud"].items() if c not in _NAMED)
    maps_total = t["by_cloud"].get("GMP", 0.0)
    vendor_total = t["by_cloud"].get("VENDOR", 0.0)
    cloud_budget = sum(v for c, v in budgets.items() if c not in _NAMED and v)
    maps_budget = budgets.get("GMP") or 0

    entries = []
    for cloud, val in sorted(t["by_cloud"].items(), key=lambda kv: kv[1], reverse=True):
        if cloud in _NAMED:
            continue
        entries.append((f"{collect.cloud_name(cloud)} All Accounts", val, budgets.get(cloud)))
    if maps_total:
        entries.append(("Maps Total", maps_total, maps_budget or None))
    if vendor_total:
        entries.append((f"{cfg.get('vendor_cost_head') or 'Vendor'} Total", vendor_total,
                        (budgets.get("VENDOR") or None)))
    entries.append(("Total", cloud_total + maps_total + vendor_total,
                    (cloud_budget + maps_budget + (budgets.get("VENDOR") or 0)) or None))

    # Mirrors the Slack root exactly, so the two can be read against each other.
    ws.write_row(r, 0, ["Account", f"Current ({rc})", f"Goal/day ({rc})", "Rate"], styles.header)
    r += 1
    for label, val, budget in entries:
        daily_goal = (budget / days) if budget else None
        ws.write(r, 0, label, styles.text); W.see(0, label)
        ws.write_number(r, 1, val, mfmt["plain"]); W.see(1, money.fmt(cfg, val))
        if daily_goal:
            diff = val - daily_goal
            pct = diff / daily_goal * 100.0
            ws.write_number(r, 2, daily_goal, mfmt["plain"])
            cell = ("+" if diff > 0 else "") + money.fmt(cfg, diff) + f" ({pct:+.0f}%)"
            ws.write(r, 3, cell,
                     wb.add_format({"align": "right", "bold": True,
                                    "font_color": "#C0392B" if diff > 0 else "#1E8449"}))
            W.see(2, money.fmt(cfg, daily_goal)); W.see(3, cell)
        else:
            ws.write(r, 2, "—", styles.text); ws.write(r, 3, "—", styles.text)
        r += 1
    ws.write(r, 0, "Total Cloud Costs", styles.total)
    ws.write_number(r, 1, t["grand_total"], mfmt["total"])
    ws.write(r, 2, "", styles.total); ws.write(r, 3, "", styles.total)
    r += 2

    # Unit economics — the number the old report led with.
    rides = report.get("rides")
    econ = collect.unit_economics(cfg, report)
    num_fmt = wb.add_format({"num_format": "#,##0"})
    if rides and rides.get("total"):
        ws.write(r, 0, "Total Rides", styles.label)
        ws.write_number(r, 1, rides["total"], num_fmt)
        r += 1
        for src, n in (rides.get("by_source") or {}).items():
            ws.write(r, 0, f"   {src}", styles.text); W.see(0, f"   {src}")
            ws.write_number(r, 1, n, num_fmt); W.see(1, f"{n:,}")
            r += 1
        r += 1

        # Cost per ride on every basis — see collect.unit_economics for why a
        # single blended figure is not reported.
        ws.write_row(r, 0, ["Cost per ride — basis", f"Cost ({rc})", "Rides", "Per ride"],
                     styles.header)
        r += 1
        for e in econ:
            ws.write(r, 0, e["label"], styles.text); W.see(0, e["label"])
            ws.write_number(r, 1, e["cost"], mfmt["plain"])
            ws.write_number(r, 2, e["rides"], num_fmt)
            ws.write_number(r, 3, e["per_ride"],
                            styles.money(wb, rc, decimals=2, bold=True))
            r += 1
        r += 1

    else:
        ws.write(r, 0, "Total Rides", styles.label)
        ws.write(r, 1, "unavailable", styles.subtitle)
        ws.write(r, 2, "ClickHouse unreachable — cost-per-ride omitted", styles.subtitle)
        r += 2

    # Monthly run-rate vs budget.
    proj = collect.monthly_projection(cfg, report)
    if proj:
        days = int(cfg.get("projection_days") or 30)
        ws.write_row(r, 0, [f"Monthly run-rate (today x {days})", f"Projected ({rc})",
                            f"Budget ({rc})", f"Over/under ({rc})", "vs Budget"], styles.header)
        r += 1
        over = wb.add_format({"num_format": "0.0%", "bold": True, "font_color": "#C0392B"})
        under = wb.add_format({"num_format": "0.0%", "font_color": "#1E8449"})
        for e in proj:
            lab = styles.total if e.get("is_total") else styles.text
            mny = mfmt["total"] if e.get("is_total") else mfmt["plain"]
            ws.write(r, 0, e["label"], lab); W.see(0, e["label"])
            ws.write_number(r, 1, e["projected"], mny); W.see(1, money.fmt(cfg, e["projected"]))
            if e["budget"]:
                W.see(2, money.fmt(cfg, e["budget"])); W.see(3, money.fmt(cfg, e["diff"]))
            if e["budget"]:
                ws.write_number(r, 2, e["budget"], mny)
                ws.write_number(r, 3, e["diff"], mny)
                ws.write_number(r, 4, e["pct"] / 100.0, over if e["pct"] > 0 else under)
            else:
                ws.write(r, 2, "—", styles.text)
                ws.write(r, 3, "—", styles.text)
                ws.write(r, 4, "—", styles.text)
            r += 1
        r += 1

    # Per-tab roll-up so the summary alone answers "which account moved".
    ws.write_row(r, 0, ["Account / Project", f"Cost ({rc})", "vs yesterday", "vs last week"], styles.header)
    r += 1
    for s in sorted(report["sections"], key=lambda x: x["total_report"], reverse=True):
        ws.write(r, 0, s["label"], styles.text); W.see(0, s["label"])
        ws.write_number(r, 1, s["total_report"], mfmt["plain"])
        c2 = _amt_pct(s["prev_total_report"], s["dod_pct"], rdec)
        c3 = _amt_pct(s["lw_total_report"], s["wow_pct"], rdec)
        ws.write(r, 2, c2, cmp_fmt); ws.write(r, 3, c3, cmp_fmt)
        W.see(2, c2); W.see(3, c3)
        r += 1

    for hdr in ("Account", "Cost per ride — basis", "Monthly run-rate (today x 30)",
                "Account / Project"):
        W.see(0, hdr)
    W.apply(ws)
    ws.freeze_panes(4, 0)


def _amt_pct(amount: float, pct, decimals: int = 0) -> str:
    """Baseline amount with its percentage change in brackets: '17,628 (+6%)'.

    Written as TEXT, which deliberately trades away numeric sorting and
    conditional formatting on these two columns — the pair reads as one fact, and
    the 7-day grid to the left keeps the sortable numbers.
    """
    if pct is None:
        return f"{amount:,.{decimals}f} (-)"
    if pct in (float("inf"), float("-inf")):
        return f"{amount:,.{decimals}f} (new)"
    return f"{amount:,.{decimals}f} ({pct:+.0f}%)"


def _write_pct_cell(ws, wb, row, col, pct_value, pct_fmt):
    """Infinite percentages (a baseline of zero) have no meaningful numeric form —
    write them as text rather than as a misleading huge number."""
    if pct_value is None or pct_value in (float("inf"), float("-inf")):
        ws.write(row, col, "new" if pct_value == float("inf") else "—")
    else:
        ws.write_number(row, col, pct_value / 100.0, pct_fmt)


def _detail_sheet(wb, styles, taken, cfg, report, section):
    """One account/project: every service as a row, the last 7 days as columns.

    A 7-day grid rather than a today/yesterday pair because a single day-pair
    cannot distinguish a real step change from ordinary weekday noise. Seeing the
    whole week makes "this has been climbing since Tuesday" and "this spikes every
    Saturday" visually obvious, which is the judgement the reader is actually
    making. The trailing columns keep the deltas that drive the alerting.
    """
    ws = wb.add_worksheet(_safe_sheet_name(section["label"], taken))
    ws.hide_gridlines(2)

    native = section["currency"]
    rc = cfg["report_currency"]
    same_ccy = native == rc
    nat_fmt = _money_formats(wb, styles, native)
    rep_fmt = _money_formats(wb, styles, rc)
    ndec = 0 if native == "INR" else 2
    cmp_fmt = wb.add_format({"align": "right"})
    cmp_total = wb.add_format({"align": "right", "bold": True, "top": 2})
    days = section["days"]
    target = report["date"]

    # Service names vary hugely between clouds ("EC2 - Other" vs "Amazon Elastic
    # Container Service for Kubernetes"); size to the actual longest one.
    longest = max([len(r["service"]) for r in section["rows"]] + [len("Service")])
    ws.set_column(0, 0, max(18, min(52, longest + 2)))
    ws.set_column(1, len(days), 13)
    ws.set_column(len(days) + 1, len(days) + 4, 19)
    ws.set_row(3, 30)          # room for the wrapped date headers

    ws.write(0, 0, f"{section['label']} — 7 days to {target.strftime('%a %d %b %Y')}", styles.title)
    sub = f"Daily cost by service, in {native}."
    if not same_ccy:
        sub += f" Totals also shown in {rc} at {cfg['usd_inr_rate']:.4f}."
    ws.write(1, 0, sub, styles.subtitle)

    day_fmt = wb.add_format({
        "bold": True, "bg_color": "#34495E", "font_color": "white", "border": 1,
        "align": "center", "valign": "vcenter", "text_wrap": True, "num_format": "ddd dd mmm",
    })
    # The target day is the one being reported on — mark it so it doesn't get lost
    # among six lookback columns.
    target_hdr = wb.add_format({
        "bold": True, "bg_color": "#1B2631", "font_color": "#F7DC6F", "border": 1,
        "align": "center", "valign": "vcenter", "text_wrap": True, "num_format": "ddd dd mmm",
    })

    r = 3
    ws.write(r, 0, "Service", styles.header)
    for i, d in enumerate(days):
        ws.write_datetime(r, 1 + i, _as_datetime(d), target_hdr if d == target else day_fmt)
    c = 1 + len(days)
    tail = ["7-day total", "vs prev day", "vs same day last week"]
    if not same_ccy:
        tail.insert(1, f"{target.strftime('%d %b')} ({rc})")
    ws.write_row(r, c, tail, styles.header)

    r += 1
    first_data_row = r

    for row in section["rows"]:
        ws.write(r, 0, row["service"], styles.wrap)
        for i, v in enumerate(row["by_day"]):
            ws.write_number(r, 1 + i, v, nat_fmt["plain"])
        c = 1 + len(days)
        ws.write_number(r, c, row["week_total"], nat_fmt["plain"]); c += 1
        if not same_ccy:
            ws.write_number(r, c, row["today_report"], rep_fmt["plain"]); c += 1
        ws.write(r, c, _amt_pct(row["yesterday"], row["dod_pct"], ndec), cmp_fmt); c += 1
        ws.write(r, c, _amt_pct(row["last_week"], row["wow_pct"], ndec), cmp_fmt)
        ws.set_row(r, None)
        r += 1

    # Total row
    bold_pct = _pct_fmt(wb, bold=True, top=2)
    ws.write(r, 0, "Total", styles.total)
    for i, v in enumerate(section["daily_totals"]):
        ws.write_number(r, 1 + i, v, nat_fmt["total"])
    c = 1 + len(days)
    ws.write_number(r, c, section["week_total_native"], nat_fmt["total"]); c += 1
    if not same_ccy:
        ws.write_number(r, c, section["total_report"], rep_fmt["total"]); c += 1
    ws.write(r, c, _amt_pct(section["prev_total_native"], section["dod_pct"], ndec), cmp_total); c += 1
    ws.write(r, c, _amt_pct(section["lw_total_native"], section["wow_pct"], ndec), cmp_total)

    if section["rows"]:
        last_data_row = r - 1
        # The comparison columns are text now, so cell-value rules cannot key off
        # them. The day-column heat map below carries the visual signal instead.
        # A colour ramp across the day columns turns the grid into a heat map —
        # a service trending up reads as a gradient without checking any number.
        ws.conditional_format(first_data_row, 1, last_data_row, len(days), {
            "type": "3_color_scale",
            "min_color": "#FFFFFF", "mid_color": "#FDEBD0", "max_color": "#E6B0AA",
        })
        ws.autofilter(first_data_row - 1, 0, last_data_row, 1 + len(days) + len(tail) - 1)

    ws.freeze_panes(first_data_row, 1)


def _as_datetime(d):
    """xlsxwriter wants a datetime for write_datetime; dates alone are rejected."""
    from datetime import datetime, time
    return datetime.combine(d, time.min)


def _gmp_sheet(wb, styles, taken, cfg, report):
    """Google Maps Platform: cost AND request volume per API.

    Requests are the point of this tab. Several Maps APIs sit inside a free tier
    and bill at zero, so a cost-only view renders 1.1M Maps API calls as a blank
    line — right up until the tier is exhausted and it becomes the biggest number
    in the report.
    """
    ws = wb.add_worksheet(_safe_sheet_name("GMP (Maps)", taken))
    ws.set_column(0, 0, 40)
    ws.set_column(1, 1, 16)
    ws.set_column(2, 4, 22)
    ws.hide_gridlines(2)

    rc = cfg["report_currency"]
    mfmt = _money_formats(wb, styles, rc)
    num = wb.add_format({"num_format": "#,##0"})
    num_total = wb.add_format({"num_format": "#,##0", "bold": True, "top": 2})
    d = report["date"]

    ws.write(0, 0, f"Google Maps Platform — {d.strftime('%a %d %b %Y')}", styles.title)
    ws.write(1, 0, "Per-API request volume and net cost after credits.", styles.subtitle)

    df = report["gmp_apis"]
    r = 3
    ws.write_row(r, 0, ["API", "Requests", f"Current cost ({rc})", "Cost per 1k requests"], styles.header)
    r += 1
    first = r

    for _, row in df.iterrows():
        requests_n = float(row["requests"] or 0)
        cost = float(row["cost"] or 0)
        ws.write(r, 0, row["service"], styles.wrap)
        ws.write_number(r, 1, requests_n, num)
        ws.write_number(r, 2, cost, mfmt["plain"])
        if requests_n > 0:
            ws.write_number(r, 3, cost / requests_n * 1000.0, styles.money(wb, rc))
        else:
            ws.write(r, 3, "—", styles.text)
        r += 1

    ws.write(r, 0, "Total", styles.total)
    ws.write_number(r, 1, float(df["requests"].fillna(0).sum()), num_total)
    ws.write_number(r, 2, float(df["cost"].fillna(0).sum()), mfmt["total"])
    ws.write(r, 3, "", styles.total)

    if len(df):
        ws.autofilter(first - 1, 0, r - 1, 3)
    ws.freeze_panes(first, 1)
