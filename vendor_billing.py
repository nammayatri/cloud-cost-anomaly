"""Third-party verification-vendor daily cost, from its dashboard billing API.

The vendor exposes no machine credential — the billing endpoint authenticates with
the dashboard's browser session cookie. A ~48h session token (`refreshToken`) is
the only one required; the short-lived access token is not. So the operational
model is: a human pastes a fresh session token into the secret every ~48h, and
every run in between reuses it.

Everything vendor-specific — the API URL, the request Origin, the client id, the
session token — comes from config/secret, never from code. That keeps the endpoint
and credentials out of this (public) repository and lets the same code target a
different tenant or a staging host without a change here.

The API accepts an arbitrary date range and returns the cost for exactly that
range, so a single day is a start==end query — no diffing of month-to-date totals.
Cost is per appId, per module+unit, already in the reporting currency.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import date

log = logging.getLogger("cost-anomaly.vendor-billing")

_TIMEOUT = 45


def configured(cfg: dict) -> bool:
    return bool(cfg.get("vendor_api_url") and cfg.get("vendor_refresh_token") and cfg.get("vendor_client_id"))


def _cookie(cfg: dict) -> str:
    # The session token is pasted into the secret by hand, so it can arrive with a
    # trailing newline or stray whitespace. An HTTP header value may not contain a
    # newline — urllib raises "Invalid header value" and the whole fetch fails — and
    # a JWT never contains internal whitespace, so stripping is always safe here.
    token = (cfg.get("vendor_refresh_token") or "").strip()
    parts = [f"refreshToken={token}"]
    cc = (cfg.get("vendor_current_credentials") or "").strip()
    if cc:
        parts.append("currentCredentials=" + urllib.parse.quote(cc))
    return "; ".join(parts)


def _service(module: str, unit: str) -> str:
    """One service label per billed line. A module bills under several units
    (ID Card Validation → OCR / Quality Checks / …), and each is a distinct cost,
    so the unit has to be part of the key or they collapse into one row."""
    module = (module or "").strip()
    unit = (unit or "").strip()
    return f"{module} · {unit}" if unit and unit != "-" else module


def fetch_day(cfg: dict, day: date) -> list[dict]:
    """Per (appId, service) vendor cost for one day, in the reporting currency.

    Returns [{"account": appId, "service": ..., "cost": float}]. Raises on HTTP or
    auth failure so the caller can decide (the daily path logs and continues; the
    backfill surfaces it).
    """
    body = json.dumps({
        "startDate": day.isoformat(),
        "endDate": day.isoformat(),
        "clientId": cfg["vendor_client_id"],
        "splitAppIdUsage": "yes",
    }).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "Cookie": _cookie(cfg)}
    if cfg.get("vendor_origin"):
        headers["Origin"] = cfg["vendor_origin"]
    req = urllib.request.Request(cfg["vendor_api_url"], data=body, headers=headers)
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    data = json.loads(resp.read())
    if data.get("status") != "success":
        raise RuntimeError(f"Vendor billing returned {data.get('status')}: {data.get('error')}")

    # Aggregate across every appId into one row per module+unit. The Control Center
    # wants the vendor as a single line item, not split by app, so the app dimension
    # is summed away here rather than stored.
    account = cfg.get("vendor_account_label") or "Vendor"
    agg: dict[str, dict] = {}
    for block in (data.get("result", {}).get("appId", {}) or {}).values():
        for u in block.get("usageRows", []) or []:
            svc = _service(u.get("moduleName", ""), u.get("unit", ""))
            slot = agg.setdefault(svc, {"cost": 0.0, "units": 0.0})
            slot["cost"] += float(u.get("cost") or 0)
            slot["units"] += float(u.get("total") or 0)   # billable usage count

    rows = []
    for svc, v in agg.items():
        # Keep every module that had activity, even at zero cost — a free-tier
        # module (e.g. Aadhaar Masking at 100k calls / ₹0) is exactly the volume
        # worth tracking, and its usage count is the leading indicator.
        if not v["cost"] and not v["units"]:
            continue
        rows.append({"account": account, "service": svc, "cost": v["cost"], "units": v["units"]})
    return rows
