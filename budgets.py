"""Monthly budgets, read from ClickHouse (cost_analytics.cost_budget).

Budgets used to live in the `MONTHLY_BUDGETS` env var, which meant a redeploy
every time finance changed a number — and, once the Control Center dashboard
started showing budget vs run-rate from the same data, two sources of truth that
drift the first time one of them is edited. The table is the shared one.

Resolution rules, kept deliberately small:

  * **Carry-forward.** A month with no row inherits the most recent earlier
    month, so a budget is entered once rather than re-entered every month. Done
    in SQL with `argMax(budget_inr, month)` over `month <= target`.

  * **Cost-head level wins.** A row with an empty `account` is the budget for the
    whole cost head. Account-level rows exist for the dashboard's finer
    breakdown; this report only needs the cloud total, so it uses the cost-head
    row when there is one and falls back to summing the account rows when there
    isn't. That way a budget entered at either grain still produces a number here.

Failure is never fatal: any problem reading the table logs a warning and returns
nothing, and the caller keeps whatever `MONTHLY_BUDGETS` already held. A budget is
context on a cost report — losing it must not cost the report itself.
"""

import json
import logging
import urllib.parse
import urllib.request
from datetime import date

import store

log = logging.getLogger("cost-anomaly.budgets")

_TIMEOUT = 30
_TABLE = "cost_budget"


def _rows(cfg: dict, month: date) -> list[dict]:
    """Carried-forward budget rows as of `month`, straight from ClickHouse."""
    db = cfg.get("cost_ch_database", "cost_analytics")
    query = (
        f"SELECT type, cost_head, account, argMax(budget_inr, month) AS budget_inr "
        f"FROM {db}.{_TABLE} FINAL "
        f"WHERE month <= {{month:Date}} "
        f"GROUP BY type, cost_head, account FORMAT JSONEachRow"
    )
    qs = urllib.parse.urlencode({"query": query, "param_month": month.isoformat()})
    scheme = "https" if cfg.get("cost_ch_secure") else "http"
    url = f"{scheme}://{cfg['cost_ch_host']}:{cfg.get('cost_ch_port', 8123)}/?{qs}"
    req = urllib.request.Request(url, headers={
        "X-ClickHouse-User": cfg["cost_ch_user"],
        "X-ClickHouse-Key": cfg["cost_ch_password"],
    })
    resp = urllib.request.urlopen(req, timeout=_TIMEOUT)
    out = []
    for line in resp.read().decode().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def resolve(rows: list[dict], cfg: dict) -> dict[str, float]:
    """Budget rows -> the report's `monthly_budgets` shape, keyed by cloud."""
    vendor = (cfg.get("vendor_type", "Data and Tools"), cfg.get("vendor_cost_head", "Vendor"))

    head_level: dict[str, float] = {}
    account_level: dict[str, float] = {}
    for row in rows:
        cloud = store.cloud_for(row["type"], row["cost_head"])
        if cloud is None and (row["type"], row["cost_head"]) == vendor:
            cloud = "VENDOR"
        if cloud is None:
            # A budget for a taxonomy this report doesn't publish. The dashboard
            # may still want it, so this is not an error — just not ours.
            log.info("Budget row for unmapped %s/%s ignored", row["type"], row["cost_head"])
            continue
        amount = float(row["budget_inr"])
        if row.get("account"):
            account_level[cloud] = account_level.get(cloud, 0.0) + amount
        else:
            head_level[cloud] = head_level.get(cloud, 0.0) + amount

    return {cloud: head_level.get(cloud, account_level.get(cloud, 0.0))
            for cloud in set(head_level) | set(account_level)}


def fetch(cfg: dict, day: date) -> dict[str, float]:
    """Budgets in force for `day`'s month, or {} if unavailable.

    {} means "no opinion" — the caller keeps the configured budgets. It never
    means "the budget is zero", which would read as wildly over plan.
    """
    if not store.configured(cfg):
        return {}
    try:
        rows = _rows(cfg, day.replace(day=1))
    except Exception as e:
        log.warning("Budget fetch failed (%s) — keeping configured budgets: %s",
                    cfg.get("cost_ch_table", _TABLE), e)
        return {}
    budgets = resolve(rows, cfg)
    if budgets:
        log.info("Budgets from ClickHouse for %s: %s", day.replace(day=1), budgets)
    return budgets
