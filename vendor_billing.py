"""Third-party verification-vendor daily cost, computed from its own request logs.

The vendor's billing *dashboard* has no machine credential — it authenticates
with a browser session cookie, and even logged in it never breaks cost down by
endpoint. What the vendor does expose is a machine-credentialed logs API: one row
per request made that day, with the endpoint it hit and the HTTP status it
returned. The vendor bills per request per endpoint, so the actual bill is
computed here — count requests per endpoint, multiply by a configured price —
rather than fetched pre-computed.

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
"""

import csv
import io
import json
import logging
import urllib.request
from datetime import date

log = logging.getLogger("cost-anomaly.vendor-billing")

_TIMEOUT = 45


def configured(cfg: dict) -> bool:
    return bool(cfg.get("vendor_logs_api_url") and cfg.get("vendor_app_id") and cfg.get("vendor_app_key"))


def _normalize_endpoint(url: str) -> str:
    """originalurl as logged sometimes carries a bare trailing '?' with no actual
    query string (the async endpoints always append it, params or not) — strip
    anything from '?' on so the same endpoint doesn't fragment into two price-table
    keys."""
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
        raise RuntimeError(f"Vendor logs API returned {data.get('status')}: {data.get('error')}")
    csv_url = (data.get("data") or {}).get("url")
    if not csv_url:
        raise RuntimeError(f"Vendor logs API response for {day} had no CSV url: {data}")

    # No app headers here: the signature baked into the presigned URL is the only
    # auth this request needs (and sending unrelated headers to S3 can turn into a
    # signature mismatch instead of being harmlessly ignored).
    csv_resp = urllib.request.urlopen(csv_url, timeout=_TIMEOUT)
    return csv_resp.read().decode("utf-8")


def _counts_by_endpoint(csv_text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        ep = _normalize_endpoint(row.get("originalurl", ""))
        if not ep:
            continue
        counts[ep] = counts.get(ep, 0) + 1
    return counts


def fetch_day(cfg: dict, day: date) -> list[dict]:
    """Per-endpoint vendor cost for one day, priced from that day's request log.

    Returns [{"account": ..., "service": endpoint, "cost": float, "units": count}].
    `cost` is `count * vendor_pricing[endpoint]`, in `report_currency` — an
    endpoint missing from `vendor_pricing` still shows up, at zero cost and its
    full request volume, rather than being dropped: a forgotten price should read
    as an obviously-wrong zero next to real volume, not vanish silently.

    Raises on HTTP or auth failure so the caller can decide (the daily path logs
    and continues; the backfill surfaces it). A day with a valid, empty log
    (genuinely zero traffic) returns [] rather than raising.
    """
    counts = _counts_by_endpoint(_fetch_csv(cfg, day))
    if not counts:
        return []

    pricing = cfg.get("vendor_pricing") or {}
    unpriced = sorted(ep for ep in counts if ep not in pricing)
    if unpriced:
        log.warning("Vendor endpoints with no configured price for %s (billed as 0): %s",
                    day, ", ".join(unpriced))

    account = cfg.get("vendor_account_label") or "Vendor"
    rows = []
    for ep, n in counts.items():
        price = float(pricing.get(ep, 0.0))
        rows.append({"account": account, "service": ep, "cost": price * n, "units": float(n)})
    return rows
