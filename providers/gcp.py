"""GCP cost provider — BigQuery billing export.

Mirrors the AWS provider's contract, but GCP differs in ways that matter:

  * The export is BILLING-ACCOUNT-WIDE even though it lives inside one project.
    Every query MUST filter project.id, or you report other projects' spend as
    your own. `gcp_projects` is required for exactly this reason.
  * Cost is reported gross; credits (CUD/SUD/promos/discounts) arrive in a
    repeated `credits` field. Net = cost + SUM(credits.amount) — credit amounts
    are already negative. Net is what actually gets invoiced, so that's what we
    report.
  * Currency is the billing account's (INR for us), not USD.
  * Rows are restated for ~24-48h after the usage day. The table is partitioned
    on _PARTITIONTIME (INGEST time), which is NOT the usage day: a restatement
    for Monday can land in Thursday's partition. So usage_start_time drives
    correctness and _PARTITIONTIME only prunes (an ingest can never predate the
    usage it describes, so >= start is safe).
  * A service maps to service.description; the AWS USAGE_TYPE drill-down
    dimension maps to sku.description.
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


def fetch_by_service(cfg: dict, end: date | None = None) -> dict[str, pd.DataFrame]:
    """Daily net cost by service, per project, for the trailing lookback_days ending at `end` (exclusive)."""
    end = end or date.today()
    start = end - timedelta(days=cfg["lookback_days"])
    projects = scopes(cfg)

    sql = f"""
        SELECT
          project.id            AS project,
          service.description   AS service,
          DATE(usage_start_time) AS day,
          {_NET_COST}           AS cost
        FROM `{cfg['gcp_billing_table']}`
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


def fetch_usage_types(cfg: dict, scope: str, service: str, end: date | None = None, days: int = 8):
    """Daily net cost + usage quantity by SKU for one service within one project."""
    end = end or date.today()
    start = end - timedelta(days=days)

    # usage.amount/usage.unit are raw ("byte-seconds", "seconds"); pricing units are
    # what the SKU is actually billed and reasoned in ("gibibyte month", "hour").
    # Using the raw pair would also trip slack._fmt_qty's substring match, which
    # sees "byte" inside "byte-seconds" and mislabels it GB.
    sql = f"""
        SELECT
          sku.description                     AS usage_type,
          DATE(usage_start_time)              AS day,
          {_NET_COST}                         AS cost,
          SUM(usage.amount_in_pricing_units)  AS qty,
          ANY_VALUE(usage.pricing_unit)       AS unit
        FROM `{cfg['gcp_billing_table']}`
        WHERE _PARTITIONTIME >= TIMESTAMP(@start)
          AND DATE(usage_start_time) >= @start
          AND DATE(usage_start_time) <  @end
          AND project.id = @project
          AND service.description = @service
        GROUP BY usage_type, day
    """
    params = [
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end),
        bigquery.ScalarQueryParameter("project", "STRING", scope),
        bigquery.ScalarQueryParameter("service", "STRING", service),
    ]
    df = _run(cfg, sql, params)
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), {}

    units = df.groupby("usage_type")["unit"].first().to_dict()

    cost_df = df.pivot_table(index="day", columns="usage_type", values="cost", aggfunc="sum").fillna(0.0)
    cost_df.index = pd.to_datetime(cost_df.index).date

    qty_df = df.pivot_table(index="day", columns="usage_type", values="qty", aggfunc="sum").fillna(0.0)
    qty_df.index = pd.to_datetime(qty_df.index).date

    return cost_df.sort_index(), qty_df.sort_index(), units


def load_csv(path: str) -> dict[str, pd.DataFrame]:
    raise NotImplementedError(
        "CSV backtesting is AWS-only. For GCP, point --date at the BigQuery export instead: "
        "the full history is already there."
    )
