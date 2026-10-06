"""Third-party verification-vendor daily cost, computed from its own request logs.

The vendor's billing *dashboard* has no machine credential — it authenticates
with a browser session cookie, and even logged in it never breaks cost down by
endpoint. What the vendor does expose is a machine-credentialed logs API: one row
per request made that day, with the endpoint it hit and the HTTP status it
returned. The vendor bills per request, per BILLING UNIT — not per endpoint —
and prices each unit on a monthly-cumulative slab (the 200,001st request this
month is cheaper than the 1st), so the actual bill is computed here rather than
fetched pre-computed.

Endpoint vs. billing unit
--------------------------
The console groups endpoints into "modules" but bills some of them under more
than one unit at once. A single `/v1/readId` call is billed as BOTH an OCR
charge and an OCR-quality-check charge — the quality check has no endpoint of
its own, it rides on the same logged request. `/v1/checkLiveness` works the
same way (liveness + a liveness-quality-check charge). `_ENDPOINT_UNITS` is
that mapping; it's structural (how the vendor's API is shaped), not something
an operator tunes, so it lives in code. `vendor_pricing` (config/secret) is the
price *per unit*, which does change and must never be hardcoded.

Everything vendor-specific — the logs API URL, the app id/key, the price table —
comes from config/secret, never from code. That keeps the endpoint, credentials
and prices out of this (public) repository and lets the same code target a
different tenant, or be repriced, without a code change.

The logs endpoint itself doesn't return the CSV: it returns a short-lived
presigned S3 URL, which must be fetched separately (unauthenticated — the
signature embedded in the URL is the auth for that second request).

Every request in the log counts toward volume, regardless of its status code.
That matches how the endpoint is priced (a request that reached the vendor costs
compute whether or not it later validated) — if the vendor's actual contract only
bills 2xx responses, `vendor_pricing` is the place to reconcile that, not this
counting logic.

The vendor's own log export has real gaps (an entire multi-month stretch has
gone missing in practice, and month-end days can lag behind month-start) — so a
day with no log file is a distinct, expected outcome, not an error to crash on.
`fetch_day_counts` raises for exactly that reason: so the caller can tell "the
vendor made zero requests that day" (empty dict) apart from "we don't know what
the vendor did that day" (exception) and never silently price the second as the
first.
"""

import csv
import io
import json
import logging
import urllib.request
from datetime import date

log = logging.getLogger("cost-anomaly.vendor-billing")

_TIMEOUT = 45

# Logged endpoint (originalurl, minus any query string) -> the billing unit
# code(s) it's charged under. An endpoint absent here is priced under its own
# raw path instead (see _unit_counts) — still visible, just unmapped.
_ENDPOINT_UNITS: dict[str, tuple[str, ...]] = {
    "/api/centralDBCheck": ("central_db_check",),
    "/v1/matchFace": ("facematch",),
    # One readId call bills as OCR *and* the OCR quality check together.
    "/v1/readId": ("ocr", "ocrQualityCheck"),
    "/api/v1/maskAadhaar": ("aadhaarMasking",),
    "/v1/async/RCVerification": ("async_RCVerification",),
    # One checkLiveness call bills as liveness *and* its quality check together.
    "/v1/checkLiveness": ("liveness", "livenessQualityCheck"),
    "/api/matchFields": ("textMatch",),
    # Two spellings map to the same unit: "/v1/async/checkDL" is what's actually
    # ever been observed in the logs; "/api/checkDL" is the name on the pricing
    # sheet. Keeping both means whichever one the vendor logs, it prices right.
    "/v1/async/checkDL": ("async_checkDL",),
    "/api/checkDL": ("async_checkDL",),
}


def configured(cfg: dict) -> bool:
    return bool(cfg.get("vendor_logs_api_url") and cfg.get("vendor_app_id") and cfg.get("vendor_app_key"))


def _normalize_endpoint(url: str) -> str:
    """originalurl as logged sometimes carries a bare trailing '?' with no actual
    query string (the async endpoints always append it, params or not) — strip
    anything from '?' on so the same endpoint doesn't fragment into two keys."""
    return (url or "").split("?", 1)[0].strip()


def _fetch_csv(cfg: dict, day: date) -> str:
    body = json.dumps({"date": day.isoformat()}).encode()
    headers = {
        "appid": cfg["vendor_app_id"],
        "appKey": cfg["vendor_app_key"],
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(cfg["vendor_logs_api_url"], data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    data = json.loads(resp.read())
    if data.get("status") != "success":
        raise RuntimeError(f"Vendor logs API returned {data.get('status')}: {data.get('message') or data.get('error')}")
    csv_url = (data.get("data") or {}).get("url")
    if not csv_url:
        raise RuntimeError(f"Vendor logs API response for {day} had no CSV url: {data}")

    # No app headers here: the signature baked into the presigned URL is the only
    # auth this request needs (and sending unrelated headers to S3 can turn into a
    # signature mismatch instead of being harmlessly ignored).
    csv_resp = urllib.request.urlopen(csv_url, timeout=_TIMEOUT)
    return csv_resp.read().decode("utf-8")


def _unit_counts(csv_text: str) -> dict[str, int]:
    """Billing-unit request counts for one day's raw log.

    One logged row can increment more than one unit's count (see
    _ENDPOINT_UNITS) — both increments use the SAME count, since the sub-unit
    charge (e.g. the OCR quality check) fires on every call its parent does,
    not on a separate request of its own.
    """
    counts: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        ep = _normalize_endpoint(row.get("originalurl", ""))
        if not ep:
            continue
        for unit in _ENDPOINT_UNITS.get(ep, (ep,)):
            counts[unit] = counts.get(unit, 0) + 1
    return counts


def fetch_day_counts(cfg: dict, day: date) -> dict[str, int]:
    """Billing-unit request counts for one day.

    Raises on HTTP/auth failure OR a day with no log file — see the module
    docstring on why that distinction matters. A day that genuinely had zero
    vendor traffic returns {} without raising.
    """
    return _unit_counts(_fetch_csv(cfg, day))


def _tier_cost(tiers: list, start_count: int, n: int) -> float:
    """Cost of the next `n` requests of one billing unit, given `start_count`
    already made this month (not counting today).

    Tiers are 1-indexed inclusive request ranges [lo, hi, price] — "0-200000"
    on the pricing sheet means requests 1 through 200,000 of the month, so a
    day whose count crosses 200,000 mid-day is split: the requests below the
    boundary price at the lower tier, the rest at the next one. `hi=None`
    means open-ended (the top tier).
    """
    if n <= 0 or not tiers:
        return 0.0
    cost = 0.0
    window_lo, window_hi = start_count + 1, start_count + n
    for lo, hi, price in tiers:
        tier_hi = hi if hi is not None else window_hi
        seg_lo, seg_hi = max(lo, window_lo), min(tier_hi, window_hi)
        if seg_lo <= seg_hi:
            cost += (seg_hi - seg_lo + 1) * price
    return cost


def price_units(cfg: dict, day_counts: dict[str, int], month_to_date: dict[str, int]) -> list[dict]:
    """Priced rows for one day, given that day's unit counts and each unit's
    cumulative count so far this month (NOT including today — see
    store._vendor_month_to_date, the source of that baseline).

    Returns [{"account", "service": unit_code, "cost", "units"}]. A unit with
    no configured tiers still shows up at zero cost and its full volume,
    rather than being dropped — a forgotten price should read as an obviously
    wrong zero next to real volume, not vanish silently.
    """
    pricing = cfg.get("vendor_pricing") or {}
    unpriced = sorted(u for u in day_counts if u not in pricing)
    if unpriced:
        log.warning("Vendor units with no configured price (billed as 0): %s", ", ".join(unpriced))

    account = cfg.get("vendor_account_label") or "Vendor"
    rows = []
    for unit, n in day_counts.items():
        tiers = pricing.get(unit)
        cost = _tier_cost(tiers, month_to_date.get(unit, 0), n) if tiers else 0.0
        rows.append({"account": account, "service": unit, "cost": cost, "units": float(n)})
    return rows
