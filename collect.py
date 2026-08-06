"""Assembles the daily report from every source into one cloud-agnostic structure.

Everything downstream (workbook.py, slack.py) reads only what this produces, so
adding a cloud means adding a section builder here and nothing else.

Target day
----------
The report covers T-2, not T-1. Both AWS and GCP restate billing rows for roughly
24-48h after the usage day, and GCP is the slower of the two. Reporting T-1 means
routinely publishing partial numbers, which produces phantom drops that vanish by
the next morning — the fastest way to teach a channel to ignore a bot. One extra
day of latency buys numbers that don't move after the fact.
"""

import logging
from datetime import date, timedelta

import pandas as pd

import money
import providers
import rides as rides_mod

log = logging.getLogger("cost-anomaly.collect")

WINDOW_DAYS = 7


def default_target(today: date | None = None) -> date:
    """T-2 — see the module docstring on billing restatement."""
    return (today or date.today()) - timedelta(days=2)


def _pct(curr: float, base: float) -> float | None:
    if base <= 0:
        return float("inf") if curr > 0 else 0.0
    return (curr - base) / base * 100.0


def _series(df, day, col):
    """Cost for one service on one day, tolerating absent days and columns."""
    if day not in df.index or col not in df.columns:
        return 0.0
    return float(df.at[day, col])


def _build_section(cfg, label, cloud, currency, df, target):
    """Turn one scope's DataFrame into a report section: 7 daily columns per service."""
    days = [target - timedelta(days=WINDOW_DAYS - 1 - i) for i in range(WINDOW_DAYS)]
    prev = target - timedelta(days=1)
    last_week = target - timedelta(days=7)
    to_report = money.rate(cfg, currency, cfg["report_currency"])

    services = [c for c in df.columns if c != "Total"]
    rows = []
    for svc in services:
        by_day = [_series(df, d, svc) for d in days]
        today_v = _series(df, target, svc)
        prev_v = _series(df, prev, svc)
        lw_v = _series(df, last_week, svc)
        # A service that spent nothing across the whole window is noise on a
        # per-service grid — it contributes seven zeroes and pushes real rows off
        # the screen.
        if not any(by_day) and today_v == 0:
            continue
        rows.append({
            "service": svc,
            "by_day": by_day,
            "week_total": sum(by_day),
            "today": today_v,
            "today_report": today_v * to_report,
            "yesterday": prev_v,
            "yesterday_report": prev_v * to_report,
            "last_week": lw_v,
            "last_week_report": lw_v * to_report,
            "dod_pct": _pct(today_v, prev_v),
            "wow_pct": _pct(today_v, lw_v),
        })

    # Biggest spender on the target day first — that is the order someone scanning
    # for "what cost us money" wants, not alphabetical.
    rows.sort(key=lambda x: x["today"], reverse=True)

    daily_totals = [sum(_series(df, d, s) for s in services) for d in days]
    total_native = sum(_series(df, target, s) for s in services)
    prev_total = sum(_series(df, prev, s) for s in services)
    lw_total = sum(_series(df, last_week, s) for s in services)

    return {
        "label": label,
        "cloud": cloud,
        "currency": currency,
        "days": days,
        "rows": rows,
        "daily_totals": daily_totals,
        "week_total_native": sum(daily_totals),
        "total_native": total_native,
        "total_report": total_native * to_report,
        "prev_total_native": prev_total,
        "prev_total_report": prev_total * to_report,
        "lw_total_native": lw_total,
        "lw_total_report": lw_total * to_report,
        "dod_pct": _pct(total_native, prev_total),
        "wow_pct": _pct(total_native, lw_total),
    }


def _aws_sections(cfg, target):
    if not cfg.get("aws_accounts"):
        return []
    aws = providers.get("aws")
    try:
        scoped = aws.fetch_by_service(cfg, end=target + timedelta(days=1))
    except Exception as e:
        log.error("AWS fetch failed: %s", e)
        return []
    out = []
    for label, df in scoped.items():
        if target not in df.index:
            log.warning("AWS %s: target %s missing (have %s..%s) — skipping",
                        label, target, df.index.min(), df.index.max())
            continue
        out.append(_build_section(cfg, label, "AWS", "USD", df, target))
    return out


def _gcp_sections(cfg, target, source, cloud, label_prefix):
    if source == "gmp" and not cfg.get("gmp_billing_table"):
        return []
    gcp = providers.get("gcp")
    try:
        scoped = gcp.fetch_by_service(cfg, end=target + timedelta(days=1), source=source)
    except Exception as e:
        log.error("GCP (%s) fetch failed: %s", source, e)
        return []
    ccy = cfg.get("currency") or "INR"
    out = []
    for project, df in scoped.items():
        if target not in df.index:
            log.warning("GCP %s: target %s missing (have %s..%s) — skipping",
                        project, target, df.index.min(), df.index.max())
            continue
        out.append(_build_section(cfg, f"{label_prefix}{project}", cloud, ccy, df, target))
    return out


def _vendor_section(cfg, target):
    """The third-party vendor as a report section: one account, one row per module
    across the 7-day window.

    The vendor API is single-day, so the window is assembled with one call per day
    (target-7 .. target — the WoW baseline needs day-7). Any failure (an expired
    session token being the usual one) drops the vendor from the live report rather
    than failing the run — the ClickHouse store is the durable record, this is
    best-effort presentation.
    """
    import vendor_billing
    if not vendor_billing.configured(cfg):
        return []
    label = cfg.get("vendor_account_label") or "Vendor"
    window = [target - timedelta(days=i) for i in range(WINDOW_DAYS)] + [target - timedelta(days=7)]
    per_day = {}
    try:
        for d in sorted(set(window)):
            per_day[d] = {r["service"]: r["cost"] for r in vendor_billing.fetch_day(cfg, d)}
    except Exception as e:
        log.error("Vendor billing fetch failed — omitting from the live report: %s", e)
        return []
    if not per_day.get(target):
        return []

    services = sorted({s for day in per_day.values() for s in day})
    rows_by_day = [{s: per_day.get(d, {}).get(s, 0.0) for s in services}
                   for d in sorted(per_day)]
    df = pd.DataFrame(rows_by_day, index=sorted(per_day))
    df["Total"] = df.sum(axis=1)
    return [_build_section(cfg, label, "VENDOR", cfg["report_currency"], df, target)]


def collect(cfg: dict, target: date | None = None) -> dict:
    target = target or default_target()
    log.info("Building report for %s (T-2)", target)

    # Must happen BEFORE any section is built — every USD figure is converted at
    # this rate as it is assembled.
    money.resolve_rate(cfg, target)

    # `provider` selects which clouds are in scope. Without this the run would
    # attempt every cloud regardless, and a deliberately AWS-only run would log
    # GCP credential errors that look like real failures.
    want = cfg.get("provider", "all")
    sections = []
    if want in ("aws", "all"):
        sections += _aws_sections(cfg, target)
    if want in ("gcp", "all"):
        sections += _gcp_sections(cfg, target, "gcp", "GCP", "GCP ")
        sections += _gcp_sections(cfg, target, "gmp", "GMP", "GMP ")
        sections += _vendor_section(cfg, target)

    if not sections:
        raise RuntimeError(f"No cost data available for {target} from any provider")

    # GMP per-API request volumes for its own tab.
    gmp_apis = None
    if want in ("gcp", "all") and cfg.get("gmp_billing_table"):
        try:
            gmp_apis = providers.get("gcp").fetch_api_usage(cfg, target, source="gmp")
        except Exception as e:
            log.warning("GMP per-API usage query failed: %s", e)

    ride_counts = rides_mod.counts_for_day(cfg, target)

    return {
        "date": target,
        "sections": sections,
        "gmp_apis": gmp_apis,
        "rides": ride_counts,
        "totals": _totals(cfg, sections),
    }


_CLOUD_NAMES = {"GMP": "Maps"}


def cloud_name(cloud: str) -> str:
    return _CLOUD_NAMES.get(cloud, cloud)


def monthly_projection(cfg: dict, report: dict) -> list[dict]:
    """Project the target day's spend to a month and compare against budget.

    The projection is deliberately the crudest possible — one day x 30. It is a
    run-rate, not a forecast: it answers "if today repeated all month, where would
    we land", which is the question a daily report can honestly answer. Anything
    cleverer (trend fitting, month-to-date extrapolation) would imply a confidence
    a single day's data does not support, and would move for reasons unrelated to
    what actually changed today.

    A day that is unrepresentative — a weekend, an incident, a backfill — will
    therefore skew it, and that is visible rather than smoothed away.
    """
    budgets = cfg.get("monthly_budgets") or {}
    if not budgets:
        return []
    days = int(cfg.get("projection_days") or 30)
    t = report["totals"]

    rows = []
    for cloud, daily in sorted(t["by_cloud"].items(), key=lambda kv: kv[1], reverse=True):
        projected = daily * days
        budget = budgets.get(cloud)
        rows.append({
            "label": cloud_name(cloud),
            "daily": daily,
            "projected": projected,
            "budget": budget,
            "pct": ((projected - budget) / budget * 100.0) if budget else None,
            "diff": (projected - budget) if budget else None,
        })

    total_daily = t["grand_total"]
    total_budget = sum(v for v in budgets.values() if v) or None
    rows.append({
        "label": "Total",
        "daily": total_daily,
        "projected": total_daily * days,
        "budget": total_budget,
        "pct": ((total_daily * days - total_budget) / total_budget * 100.0)
               if total_budget else None,
        "diff": (total_daily * days - total_budget) if total_budget else None,
        "is_total": True,
    })
    return rows


def unit_economics(cfg: dict, report: dict) -> list[dict]:
    """Cost per ride, for cloud and Maps, with and without ticket bookings.

    Two numerators, because they behave differently: infrastructure is a capacity
    bill that moves with servers, Maps is an external per-request bill that moves
    with rider behaviour. Two denominators, because ride-hailing trips and ticket
    bookings are different products sharing one platform.

    Every account of each cloud counts toward the numerator — including non-prod.
    That is a deliberate choice: the ratio is "what the platform costs us per
    trip", and sandbox spend is a real cost of running the platform.
    """
    rides = report.get("rides")
    if not rides or not rides.get("total"):
        return []

    t = report["totals"]
    by_source = rides.get("by_source") or {}
    primary = cfg.get("primary_ride_source") or next(iter(by_source), None)
    rides_only = by_source.get(primary, rides["total"])
    total_rides = rides["total"]
    others = [l for l in by_source if l != primary]
    incl = f"incl {', '.join(others)}" if others else None
    both = bool(incl) and total_rides != rides_only

    cloud = sum(v for c, v in t["by_cloud"].items() if c not in ("GMP", "VENDOR"))
    maps = t["by_cloud"].get("GMP", 0.0)

    rows = []

    def add(label, cost, n):
        if n and cost:
            rows.append({"label": label, "cost": cost, "rides": n, "per_ride": cost / n})

    add("Total Cloud Cost", cloud, rides_only)
    if both:
        add(f"Total Cloud Cost ({incl})", cloud, total_rides)
    add("Total Maps Cost", maps, rides_only)
    if both:
        add(f"Total Maps Cost ({incl})", maps, total_rides)
    add("Total Cloud + Maps Cost", cloud + maps, rides_only)
    if both:
        add(f"Total Cloud + Maps Cost ({incl})", cloud + maps, total_rides)
    return rows


def _totals(cfg, sections):
    """Roll sections up per cloud and overall, in the reporting currency.

    Percentages are recomputed from summed absolutes rather than averaged across
    sections — averaging would weight a sandbox project the same as prod.

    """
    by_cloud: dict[str, float] = {}
    by_cloud_lw: dict[str, float] = {}
    for s in sections:
        by_cloud[s["cloud"]] = by_cloud.get(s["cloud"], 0.0) + s["total_report"]
        by_cloud_lw[s["cloud"]] = by_cloud_lw.get(s["cloud"], 0.0) + s["lw_total_report"]


    grand = sum(by_cloud.values())
    buckets = []
    for cloud, total in sorted(by_cloud.items(), key=lambda kv: kv[1], reverse=True):
        buckets.append({
            "label": f"Total {cloud} Costs",
            "cloud": cloud,
            "total": total,
            "lw_total": by_cloud_lw.get(cloud, 0.0),
            "wow_pct": _pct(total, by_cloud_lw.get(cloud, 0.0)),
        })

    return {
        "buckets": buckets,
        "by_cloud": by_cloud,
        "grand_total": grand,
        "grand_total_lw": sum(by_cloud_lw.values()),
        "grand_wow_pct": _pct(grand, sum(by_cloud_lw.values())),
    }
