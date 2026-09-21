"""Write the daily per-service cost into ClickHouse (cost_analytics.cost_daily).

This is the historical store the Control Center reads from. The report itself is
transient — a Slack message and a workbook — so nothing remembers what yesterday
cost unless it is persisted. This module is that persistence.

Design points:

  * The table is ReplicatedReplacingMergeTree keyed on the dimension tuple, with
    `inserted_at` as the version. AWS and GCP restate billing for 24-48h, so a
    re-run of a past day MUST replace its rows, not append — otherwise the store
    double-counts. Re-inserting the same (date, type, cost_head, account, service)
    is therefore safe: the newest row wins after merge. Reads should use FINAL (or
    argMax(x, inserted_at)) to see the collapsed value before a merge lands.

  * Every row carries native cost + fx_rate + inr cost. Storing the rate rather
    than only the converted figure keeps a later FX correction auditable instead
    of silently rewriting history.

  * Writes go over the HTTP interface with a dedicated `cost_writer` user scoped
    to this one database — the ride-count reads still use the read-only path.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, timedelta

import money
import providers
import vendor_billing

log = logging.getLogger("cost-anomaly.store")

_TIMEOUT = 120

# cloud -> (type, cost_head). `type` is the broad Control-Center grouping,
# `cost_head` the billing bucket within it. Kept here, not in the providers, so
# the taxonomy is one edit away without touching cost logic.
_CLOUD_MAP = {
    "AWS": ("Cloud", "AWS Cost"),
    "GCP": ("Cloud", "GCP Cost"),
    "GMP": ("Maps", "Maps Cost"),
}


def cloud_for(type_: str, cost_head: str) -> str | None:
    """Inverse of _CLOUD_MAP: the report's cloud key for a stored row's taxonomy.

    Exposed so budgets.py can map table rows back to cloud keys without either
    module owning a second copy of the mapping.
    """
    for cloud, pair in _CLOUD_MAP.items():
        if pair == (type_, cost_head):
            return cloud
    return None


def configured(cfg: dict) -> bool:
    return bool(cfg.get("cost_ch_host") and cfg.get("cost_ch_user")
               and cfg.get("cost_ch_password"))


def _account(section_label: str) -> str:
    """Account column: the scope, with the cloud prefix stripped — the cloud is
    already captured by type/cost_head, so 'GCP <project>' stores as '<project>'."""
    for pfx in ("GCP ", "GMP ", "AWS "):
        if section_label.startswith(pfx):
            return section_label[len(pfx):]
    return section_label


def _row(day: date, cloud: str, account: str, service: str,
         cost_native: float, currency: str, fx_rate: float,
         units: float | None = None, type_head: tuple | None = None,
         cost_invoiced_native: float | None = None) -> dict:
    typ, head = type_head or _CLOUD_MAP.get(cloud, ("Other", cloud))
    # No credits on this source (the vendor bill) => invoiced equals usage.
    invoiced = cost_native if cost_invoiced_native is None else cost_invoiced_native
    return {
        "date": day.isoformat(),
        "type": typ,
        "cost_head": head,
        "account": _account(account),
        "service": service,
        "cost_native": round(cost_native, 6),
        "currency": currency,
        "fx_rate": round(fx_rate, 6),
        "cost_inr": round(cost_native * fx_rate, 6),
        "cost_native_invoiced": round(invoiced, 6),
        "cost_inr_invoiced": round(invoiced * fx_rate, 6),
        "units": units,
    }


def _vendor_month_to_date(cfg: dict, day: date) -> dict[str, int]:
    """Sum of `units` already recorded this month, per billing unit, for every
    day strictly before `day`. This is the baseline a tiered price needs to
    know which slab today's requests fall into.

    Reads FINAL (see module docstring) so an unmerged duplicate row from a
    same-day re-run never double-counts the baseline. Returns {} — never
    raises — if the store isn't configured or the query fails; the caller
    then prices today's count entirely in the lowest tier, which undercounts
    rather than blocking the run on a ClickHouse hiccup.
    """
    if not configured(cfg):
        return {}
    month_start = day.replace(day=1)
    db = cfg.get("cost_ch_database", "cost_analytics")
    tbl = cfg.get("cost_ch_table", "cost_daily")
    query = (
        f"SELECT service, sum(units) AS mtd FROM {db}.{tbl} FINAL "
        f"WHERE cost_head = {{cost_head:String}} AND type = {{vtype:String}} "
        f"AND date >= {{start:Date}} AND date < {{day:Date}} "
        f"GROUP BY service FORMAT JSONEachRow"
    )
    qs = urllib.parse.urlencode({
        "query": query,
        "param_cost_head": cfg.get("vendor_cost_head", "Vendor"),
        "param_vtype": cfg.get("vendor_type", "Data and Tools"),
        "param_start": month_start.isoformat(),
        "param_day": day.isoformat(),
    })
    scheme = "https" if cfg.get("cost_ch_secure") else "http"
    url = f"{scheme}://{cfg['cost_ch_host']}:{cfg.get('cost_ch_port', 8123)}/?{qs}"
    req = urllib.request.Request(url, headers={
        "X-ClickHouse-User": cfg["cost_ch_user"],
        "X-ClickHouse-Key": cfg["cost_ch_password"],
    })
    try:
        resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
        out = {}
        for line in resp.read().decode().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            out[row["service"]] = int(float(row["mtd"]))
        return out
    except Exception as e:
        log.warning("Vendor month-to-date query for %s failed (%s) — pricing today's "
                    "volume entirely in the lowest tier", day, e)
        return {}


def vendor_priced_day(cfg: dict, day: date) -> list[dict] | None:
    """Priced vendor rows for one day: [{"account","service","cost","units"}].

    Returns None — distinct from a real [] — when the day's log is
    unavailable (vendor fetch failure; the vendor's export has real gaps, see
    vendor_billing's module docstring). A day with a valid, empty log
    (genuinely zero traffic) returns [] instead. Callers must not conflate the
    two: treating "we don't know" as "zero" would understate every later day's
    tier once volume crosses a slab boundary.
    """
    if not vendor_billing.configured(cfg):
        return []
    try:
        counts = vendor_billing.fetch_day_counts(cfg, day)
    except Exception as e:
        log.warning("Vendor request log for %s unavailable: %s", day, e)
        return None
    if not counts:
        return []
    mtd = _vendor_month_to_date(cfg, day)
    return vendor_billing.price_units(cfg, counts, mtd)


def _vendor_rows(cfg: dict, day) -> list[dict]:
    """Vendor per-unit rows for one day, if configured. Reporting currency, fx
    1.0. Never raises — an unavailable day must not fail the whole write."""
    hits = vendor_priced_day(cfg, day)
    if not hits:
        return []
    th = (cfg.get("vendor_type", "Data and Tools"), cfg.get("vendor_cost_head", "Vendor"))
    return [_row(day, "VENDOR", h["account"], h["service"], h["cost"],
                 cfg["report_currency"], 1.0, units=h.get("units"), type_head=th) for h in hits]


_COLUMNS_CACHE: dict[str, set[str]] = {}


def _table_columns(cfg: dict) -> set[str]:
    """Column names of the target table, read once per process.

    Lets the code ship before the invoiced columns are added: rows are filtered to
    what the table actually has, so a pre-migration table takes the same write it
    always did instead of failing with UNKNOWN_IDENTIFIER.
    """
    db = cfg.get("cost_ch_database", "cost_analytics")
    tbl = cfg.get("cost_ch_table", "cost_daily")
    key = f"{db}.{tbl}"
    if key in _COLUMNS_CACHE:
        return _COLUMNS_CACHE[key]
    scheme = "https" if cfg.get("cost_ch_secure") else "http"
    url = (f"{scheme}://{cfg['cost_ch_host']}:{cfg.get('cost_ch_port', 8123)}/"
           f"?query=" + urllib.parse.quote(f"SELECT name FROM system.columns "
                                           f"WHERE database = '{db}' AND table = '{tbl}'"))
    req = urllib.request.Request(url, headers={
        "X-ClickHouse-User": cfg["cost_ch_user"],
        "X-ClickHouse-Key": cfg["cost_ch_password"],
    })
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    cols = {line.strip() for line in resp.read().decode().splitlines() if line.strip()}
    _COLUMNS_CACHE[key] = cols
    return cols


def _insert(cfg: dict, rows: list[dict]) -> int:
    """Bulk INSERT via JSONEachRow. Returns the number of rows written."""
    if not rows:
        return 0
    try:
        cols = _table_columns(cfg)
    except Exception as e:
        # Probe failure must not lose the write; send every key and let ClickHouse
        # decide. A pre-migration table then fails loudly, which is the right signal.
        log.warning("Could not read %s columns (%s) — inserting all fields",
                    cfg.get("cost_ch_table", "cost_daily"), e)
        cols = None
    if cols:
        rows = [{k: v for k, v in r.items() if k in cols} for r in rows]
    db = cfg.get("cost_ch_database", "cost_analytics")
    tbl = cfg.get("cost_ch_table", "cost_daily")
    scheme = "https" if cfg.get("cost_ch_secure") else "http"
    url = (f"{scheme}://{cfg['cost_ch_host']}:{cfg.get('cost_ch_port', 8123)}/"
           f"?query=" + urllib.parse.quote(f"INSERT INTO {db}.{tbl} FORMAT JSONEachRow"))
    body = "\n".join(json.dumps(r) for r in rows).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "X-ClickHouse-User": cfg["cost_ch_user"],
        "X-ClickHouse-Key": cfg["cost_ch_password"],
        "Content-Type": "application/x-ndjson",
    })
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    resp.read()
    return len(rows)


def _rows_from_report(cfg: dict, report: dict) -> list[dict]:
    """One row per (section, service) for the report's target day.

    Zero-cost services are dropped — a store row per idle SKU per day is noise the
    Control Center would have to filter out on every query.
    """
    day = report["date"]
    rows = []
    for s in report["sections"]:
        # The vendor section is persisted separately by _vendor_rows — with its own
        # cost_head/type and per-module usage units, freshly fetched. Letting it also
        # flow through here would double-write the day: once under the configured
        # vendor cost_head and again under the generic fallback ("Other"/"VENDOR").
        if s["cloud"] == "VENDOR":
            continue
        fx = money.rate(cfg, s["currency"], cfg["report_currency"])
        for r in s["rows"]:
            if not r["today"]:
                continue
            rows.append(_row(day, s["cloud"], s["label"], r["service"],
                             r["today"], s["currency"], fx,
                             cost_invoiced_native=r["today_invoiced"]))
    return rows


def _fx_series(cfg: dict, start: date, end: date) -> dict:
    """USD->INR for each day in [start, end), fetched one day at a time.

    Backfill converts each historical day at that day's own rate, not today's —
    one rate across two months would misstate the older weeks. The single-day
    endpoint is used per day rather than the range/timeseries form: the range form
    is blocked (403) on the host we can reach, while the single-day form is the
    exact call the daily run already relies on. Missing days fall back to the
    pinned rate; the caller logs which.
    """
    if cfg["report_currency"] == "USD" or not cfg.get("fx_fetch", True):
        # fx_fetch off => every day uses the pinned usd_inr_rate. Skip the network
        # entirely rather than issuing (and timing out on) a call per day.
        return {}
    tmpl = cfg.get("fx_api_url") or "https://api.frankfurter.app/{date}?from=USD&to=INR"
    out = {}
    d = start
    while d < end:
        try:
            resp = urllib.request.urlopen(tmpl.format(date=d.isoformat()), timeout=30)
            data = json.loads(resp.read().decode())
            rate = float(data["rates"]["INR"])
            if 50.0 <= rate <= 200.0:          # same sanity band as money.resolve_rate
                out[d] = rate
        except Exception as e:
            log.warning("FX fetch for %s failed: %s", d, e)
        d += timedelta(days=1)
    return out


def _vendor_backfill_rows(cfg: dict, start: date, end: date) -> list[dict]:
    """Vendor rows for [start, end), pricing each day against a running
    month-to-date total tracked IN MEMORY as the loop advances.

    This can't reuse _vendor_month_to_date per day the way the live daily path
    does: backfill inserts everything in one bulk _insert at the very end (see
    backfill()), so a same-run earlier day's units are not yet in ClickHouse
    when a later day in the same backfill would need them as its baseline —
    querying per day would silently price every day as if it were the 1st of
    the month. The running total is seeded from ClickHouse once per month
    boundary crossed (via _vendor_month_to_date), which correctly picks up
    real prior history for a backfill that starts mid-month, then advances
    in memory from there.

    A day whose log is unavailable is skipped (not zero-priced) and does not
    advance the running total, consistent with vendor_priced_day's contract.
    """
    th = (cfg.get("vendor_type", "Data and Tools"), cfg.get("vendor_cost_head", "Vendor"))
    rows: list[dict] = []
    running: dict[str, int] = {}
    seeded_month: tuple[int, int] | None = None
    unavailable: list[date] = []

    d = start
    while d < end:
        month_key = (d.year, d.month)
        if month_key != seeded_month:
            running = _vendor_month_to_date(cfg, d)
            seeded_month = month_key
        try:
            day_counts = vendor_billing.fetch_day_counts(cfg, d)
        except Exception as e:
            log.warning("Vendor request log for %s unavailable during backfill: %s", d, e)
            unavailable.append(d)
            d += timedelta(days=1)
            continue
        if day_counts:
            priced = vendor_billing.price_units(cfg, day_counts, running)
            rows += [_row(d, "VENDOR", h["account"], h["service"], h["cost"],
                          cfg["report_currency"], 1.0, units=h.get("units"), type_head=th)
                     for h in priced]
            for unit, n in day_counts.items():
                running[unit] = running.get(unit, 0) + n
        d += timedelta(days=1)

    if unavailable:
        log.warning("Vendor log unavailable for %d day(s) during backfill: %s",
                    len(unavailable), ", ".join(x.isoformat() for x in unavailable))
    return rows


def backfill(cfg: dict, start: date, end: date) -> dict:
    """Populate cost_daily for [start, end) from each provider's own history.

    Fetches each source ONCE over the whole window (the providers already return a
    date-indexed frame), rather than re-collecting per day — one set of Cost
    Explorer / BigQuery calls instead of thirty. Returns a per-source row count for
    validation.

    Re-running is safe: ReplacingMergeTree collapses duplicate dimension tuples on
    the next merge, keeping the latest inserted_at.
    """
    if not configured(cfg):
        raise RuntimeError("Cost store not configured — cannot backfill")

    fx = _fx_series(cfg, start, end)
    pinned = float(cfg["usd_inr_rate"])
    fx_misses: set[date] = set()

    def rate_for(day: date, currency: str) -> float:
        if currency == cfg["report_currency"]:
            return 1.0
        if currency == "USD":
            r = fx.get(day)
            if r is None:
                fx_misses.add(day)
                return pinned
            return r
        return money.rate(cfg, currency, cfg["report_currency"])

    # Widen the lookback so a single fetch spans the whole window.
    window = (end - start).days + 2
    cfg = {**cfg, "lookback_days": max(cfg.get("lookback_days", 21), window)}

    def emit(scoped: dict, cloud: str, currency: str) -> list[dict]:
        out = []
        for label, fr in scoped.items():
            usage, inv = fr["usage"], fr["invoiced"]
            for day in usage.index:
                if not (start <= day < end):
                    continue
                r = rate_for(day, currency)
                for svc in usage.columns:
                    if svc == "Total":
                        continue
                    v = float(usage.at[day, svc])
                    if not v:
                        continue
                    iv = float(inv.at[day, svc]) if (day in inv.index
                                                     and svc in inv.columns) else v
                    out.append(_row(day, cloud, label, svc, v, currency, r,
                                    cost_invoiced_native=iv))
        return out

    counts: dict[str, int] = {}
    want = cfg.get("provider", "all")

    if vendor_billing.configured(cfg):
        counts["vendor"] = _insert(cfg, _vendor_backfill_rows(cfg, start, end))

    if want in ("aws", "all") and cfg.get("aws_accounts"):
        rows = emit(providers.get("aws").fetch_by_service(cfg, end=end), "AWS", "USD")
        counts["AWS"] = _insert(cfg, rows)
    if want in ("gcp", "all"):
        gcp = providers.get("gcp")
        ccy = cfg.get("currency") or "INR"
        rows = emit(gcp.fetch_by_service(cfg, end=end, source="gcp"), "GCP", ccy)
        counts["GCP"] = _insert(cfg, rows)
        if cfg.get("gmp_billing_table"):
            rows = emit(gcp.fetch_by_service(cfg, end=end, source="gmp"), "GMP", ccy)
            counts["GMP"] = _insert(cfg, rows)

    if fx_misses:
        log.warning("%d day(s) had no FX quote and used the pinned rate %.4f: %s",
                    len(fx_misses), pinned, ", ".join(sorted(d.isoformat() for d in fx_misses)))
    log.info("Backfill %s..%s wrote: %s", start, end, counts)
    return counts


def write_report(cfg: dict, report: dict) -> None:
    """Persist one collected report's target day. Called by the daily cron.

    Failure is logged, not raised: the store is secondary to the Slack/Xyne
    delivery, and a ClickHouse hiccup should not fail a run that already posted.
    """
    if not configured(cfg):
        log.info("Cost store not configured — skipping ClickHouse write")
        return
    rows = _rows_from_report(cfg, report) + _vendor_rows(cfg, report["date"])
    try:
        n = _insert(cfg, rows)
        log.info("Wrote %d cost rows to ClickHouse for %s", n, report["date"])
    except Exception as e:
        log.error("ClickHouse cost write failed: %s", e)
