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
         units: float | None = None, type_head: tuple | None = None) -> dict:
    typ, head = type_head or _CLOUD_MAP.get(cloud, ("Other", cloud))
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
        "units": units,
    }


def _vendor_rows(cfg: dict, day) -> list[dict]:
    """Vendor per-module rows for one day, if configured. Reporting currency, fx
    1.0. Never raises — a session-token expiry must not fail the whole write."""
    if not vendor_billing.configured(cfg):
        return []
    try:
        hits = vendor_billing.fetch_day(cfg, day)
    except Exception as e:
        log.error("Vendor billing fetch for %s failed (token expired?): %s", day, e)
        return []
    th = (cfg.get("vendor_type", "Data and Tools"), cfg.get("vendor_cost_head", "Vendor"))
    return [_row(day, "VENDOR", h["account"], h["service"], h["cost"],
                 cfg["report_currency"], 1.0, units=h.get("units"), type_head=th) for h in hits]


def _insert(cfg: dict, rows: list[dict]) -> int:
    """Bulk INSERT via JSONEachRow. Returns the number of rows written."""
    if not rows:
        return 0
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
                             r["today"], s["currency"], fx))
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
        for label, df in scoped.items():
            for day in df.index:
                if not (start <= day < end):
                    continue
                r = rate_for(day, currency)
                for svc in df.columns:
                    if svc == "Total":
                        continue
                    v = float(df.at[day, svc])
                    if not v:
                        continue
                    out.append(_row(day, cloud, label, svc, v, currency, r))
        return out

    counts: dict[str, int] = {}
    want = cfg.get("provider", "all")

    if vendor_billing.configured(cfg):
        vend = []
        d = start
        while d < end:
            vend += _vendor_rows(cfg, d)
            d += timedelta(days=1)
        counts["vendor"] = _insert(cfg, vend)

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
