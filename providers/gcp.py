"""GCP cost provider — BigQuery billing export.

Mirrors the AWS provider's contract, but GCP differs in ways that matter:

  * The export is BILLING-ACCOUNT-WIDE even though it lives inside one project.
    Every query MUST filter project.id, or you report other projects' spend as
    your own. `gcp_projects` is required for exactly this reason.
  * Cost is reported gross; credits (CUD/SUD/promos/discounts) arrive in a
    repeated `credits` field, with negative amounts. We report cost + credits
    EXCEPT promotions: a promotional grant can cover the entire invoice (one
    started 2026-09-15), which drives the invoiced total to zero and makes every
    service look free. See _USAGE_COST.
  * Currency is the billing account's, not USD.
  * Rows are restated for ~24-48h after the usage day. The table is partitioned
    on _PARTITIONTIME (INGEST time), which is NOT the usage day: a restatement
    for Monday can land in Thursday's partition. So usage_start_time drives
    correctness and _PARTITIONTIME only prunes (an ingest can never predate the
    usage it describes, so >= start is safe).
"""

from datetime import date, timedelta

import pandas as pd
from google.cloud import bigquery


def scopes(cfg: dict) -> list[str]:
    """Ordered — the first project is the one that appears at the top of the report."""
    projects = cfg.get("gcp_projects") or []
    if not projects:
        raise RuntimeError(
            "gcp_projects is empty. The billing export covers the whole billing "
            "account, so an explicit project list is required."
        )
    return list(projects)


def _source(cfg: dict, source: str) -> tuple[str, list[str]]:
    """Resolve which billing export to read.

    Google Maps Platform bills through a separate billing account and therefore a
    separate export table, but the schema is byte-identical to the infra export —
    so one set of queries serves both, parameterised only by table + project list.
    """
    if source == "gmp":
        table = cfg.get("gmp_billing_table")
        projects = cfg.get("gmp_projects") or []
        if not table:
            raise RuntimeError("gmp_billing_table is not configured")
        if not projects:
            raise RuntimeError("gmp_projects is empty — the GMP export is billing-account-wide too")
        return table, list(projects)
    return cfg["gcp_billing_table"], scopes(cfg)


def currency(cfg: dict) -> str:
    return cfg.get("currency") or "INR"


def _client(cfg: dict) -> bigquery.Client:
    return bigquery.Client(project=cfg.get("gcp_project") or None)


# cost + credits => what the usage costs. Credit amounts are negative, so this
# subtracts.
#
# PROMOTION credits are excluded. They are billing-account-wide grants that cover
# the whole invoice — the current one started 2026-09-15 — so including them
# reports every service at ~0 and hides real movement. Everything that reflects
# what the usage actually costs (COMMITTED_USAGE_DISCOUNT, DISCOUNT, SUD) is kept.
#
# The `Invoice / Contract billing adjustment` row must be dropped with them: it is
# a positive cost line carrying an exactly offsetting PROMOTION credit, so once
# promotions are excluded it would inflate the total by that same amount
# (₹19k-36k/day in Sept 2026).
_USAGE_COST = """SUM(IF(service.description = 'Invoice', 0,
            cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c
                           WHERE c.type != 'PROMOTION'), 0)))"""

# Invoiced: cost after EVERY credit, including promotions, and including the
# Invoice adjustment row. This is the number finance sees on the bill.
_INVOICED_COST = "SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0))"


def _run(cfg: dict, sql: str, params: list) -> pd.DataFrame:
    client = _client(cfg)
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    )
    return job.result().to_dataframe()


def _pivot(sub, value_col: str):
    """Long rows -> date-indexed frame, one column per service, plus 'Total'."""
    pivot = sub.pivot_table(index="day", columns="service", values=value_col,
                            aggfunc="sum").fillna(0.0)
    pivot.index = pd.to_datetime(pivot.index).date
    pivot = pivot.sort_index()
    pivot["Total"] = pivot.sum(axis=1)
    return pivot


def fetch_by_service(cfg: dict, end: date | None = None, source: str = "gcp") -> dict[str, dict]:
    """Daily cost by service, per project, on both bases, for the trailing
    lookback_days ending at `end` (exclusive)."""
    end = end or date.today()
    start = end - timedelta(days=cfg["lookback_days"])
    table, projects = _source(cfg, source)

    sql = f"""
        SELECT
          project.id            AS project,
          service.description   AS service,
          DATE(usage_start_time) AS day,
          {_USAGE_COST}         AS cost,
          {_INVOICED_COST}      AS cost_invoiced
        FROM `{table}`
        WHERE _PARTITIONTIME >= TIMESTAMP(@start)
          AND DATE(usage_start_time) >= @start
          AND DATE(usage_start_time) <  @end
          AND project.id IN UNNEST(@projects)
        GROUP BY project, service, day
    """
    params = [
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end),
        bigquery.ArrayQueryParameter("projects", "STRING", projects),
    ]
    df = _run(cfg, sql, params)
    if df.empty:
        return {}

    out: dict[str, dict] = {}
    for project in projects:  # iterate the configured order, not what BQ returned
        sub = df[df["project"] == project]
        if sub.empty:
            continue
        out[project] = {
            "usage": _pivot(sub, "cost"),
            "invoiced": _pivot(sub, "cost_invoiced"),
        }
    return out


def fetch_account_daily(cfg: dict, end: date | None = None, source: str = "gcp") -> pd.DataFrame:
    """Daily cost for the WHOLE billing account — every project, configured or not.

    `fetch_by_service` deliberately filters to `gcp_projects`, so anything billed to
    a project nobody listed is invisible. This is the account-level total to
    reconcile against the invoice, and to catch spend in a project that was never
    added to the config.

    Returns a DataFrame of (day, billing_account_id, cost), oldest day first.
    """
    end = end or date.today()
    start = end - timedelta(days=cfg["lookback_days"])
    table, _ = _source(cfg, source)

    sql = f"""
        SELECT
          DATE(usage_start_time) AS day,
          billing_account_id     AS billing_account_id,
          {_USAGE_COST}          AS cost
        FROM `{table}`
        WHERE _PARTITIONTIME >= TIMESTAMP(@start)
          AND DATE(usage_start_time) >= @start
          AND DATE(usage_start_time) <  @end
        GROUP BY day, billing_account_id
        ORDER BY day
    """
    params = [
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end),
    ]
    return _run(cfg, sql, params)


def fetch_api_usage(cfg: dict, day: date, source: str = "gmp") -> pd.DataFrame:
    """Per-API request counts and net cost for a single day.

    This is the GMP view the old report led with: one row per Maps API with the
    number of requests next to the rupees. Request counts matter more than cost
    here — a free-tier API showing 1.1M calls is the thing that becomes expensive
    the moment the tier is exhausted, and cost alone would render it as 0.

    Returns a DataFrame of (service, requests, cost), busiest spender first.
    """
    table, projects = _source(cfg, source)
    sql = f"""
        SELECT
          service.description                 AS service,
          {_USAGE_COST}                       AS cost,
          SUM(usage.amount_in_pricing_units)  AS requests
        FROM `{table}`
        WHERE _PARTITIONTIME >= TIMESTAMP(@day)
          AND DATE(usage_start_time) = @day
          AND project.id IN UNNEST(@projects)
        GROUP BY service
        ORDER BY cost DESC, requests DESC
    """
    params = [
        bigquery.ScalarQueryParameter("day", "DATE", day),
        bigquery.ArrayQueryParameter("projects", "STRING", projects),
    ]
    df = _run(cfg, sql, params)
    if df.empty:
        return pd.DataFrame(columns=["service", "cost", "requests"])
    return df
