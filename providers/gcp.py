"""GCP cost provider — BigQuery billing export.

Mirrors the AWS provider's contract, but GCP differs in ways that matter:

  * The export is BILLING-ACCOUNT-WIDE even though it lives inside one project.
    Every query MUST filter project.id, or you report other projects' spend as
    your own. `gcp_projects` is required for exactly this reason.
  * Cost is reported gross; credits (CUD/SUD/promos/discounts) arrive in a
    repeated `credits` field. Net = cost + SUM(credits.amount) — credit amounts
    are already negative. Net is what actually gets invoiced, so that's what we
    report.
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


# cost + credits => net. Credit amounts are negative, so this subtracts.
_NET_COST = "SUM(cost + IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0))"


def _run(cfg: dict, sql: str, params: list) -> pd.DataFrame:
    client = _client(cfg)
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    )
    return job.result().to_dataframe()


def fetch_by_service(cfg: dict, end: date | None = None, source: str = "gcp") -> dict[str, pd.DataFrame]:
    """Daily net cost by service, per project, for the trailing lookback_days ending at `end` (exclusive)."""
    end = end or date.today()
    start = end - timedelta(days=cfg["lookback_days"])
    table, projects = _source(cfg, source)

    sql = f"""
        SELECT
          project.id            AS project,
          service.description   AS service,
          DATE(usage_start_time) AS day,
          {_NET_COST}           AS cost
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

    out: dict[str, pd.DataFrame] = {}
    for project in projects:  # iterate the configured order, not what BQ returned
        sub = df[df["project"] == project]
        if sub.empty:
            continue
        pivot = sub.pivot_table(index="day", columns="service", values="cost", aggfunc="sum").fillna(0.0)
        pivot.index = pd.to_datetime(pivot.index).date
        pivot = pivot.sort_index()
        pivot["Total"] = pivot.sum(axis=1)
        out[project] = pivot
    return out


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
          {_NET_COST}                         AS cost,
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
