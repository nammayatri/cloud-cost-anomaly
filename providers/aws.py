"""AWS cost provider — Cost Explorer (ce:GetCostAndUsage).

Cost Explorer is already scoped to the credentialed account, so there is exactly
one scope. The scope name is cosmetic (it only labels the Slack section).
"""

from datetime import date, timedelta

import boto3
import pandas as pd

_SCOPE = "AWS"


def scopes(cfg: dict) -> list[str]:
    return [_SCOPE]


def currency(cfg: dict) -> str:
    return "USD"


def _client(cfg: dict):
    session = boto3.Session(
        profile_name=cfg.get("aws_profile") or None,
        region_name=cfg.get("aws_region"),
    )
    return session.client("ce")


def _paginate(ce, **kwargs):
    pages = []
    token = None
    while True:
        if token:
            kwargs["NextPageToken"] = token
        resp = ce.get_cost_and_usage(**kwargs)
        pages.append(resp)
        token = resp.get("NextPageToken")
        if not token:
            break
    return pages


def fetch_by_service(cfg: dict, end: date | None = None) -> dict[str, pd.DataFrame]:
    """Daily UnblendedCost grouped by SERVICE for the trailing lookback_days, ending at `end` (exclusive)."""
    end = end or date.today()
    start = end - timedelta(days=cfg["lookback_days"])

    ce = _client(cfg)
    pages = _paginate(
        ce,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    rows = []
    for page in pages:
        for day in page["ResultsByTime"]:
            d = day["TimePeriod"]["Start"]
            for grp in day["Groups"]:
                svc = grp["Keys"][0]
                amt = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                rows.append((d, svc, amt))

    if not rows:
        return {}

    df = pd.DataFrame(rows, columns=["date", "service", "cost"])
    pivot = df.pivot_table(index="date", columns="service", values="cost", aggfunc="sum").fillna(0.0)
    pivot.index = pd.to_datetime(pivot.index).date
    pivot = pivot.sort_index()
    pivot["Total"] = pivot.sum(axis=1)
    return {_SCOPE: pivot}


def fetch_usage_types(cfg: dict, scope: str, service: str, end: date | None = None, days: int = 8):
    """Daily cost+quantity by USAGE_TYPE for a single service.

    `scope` is accepted for interface parity and ignored — CE is single-account.
    Returns (cost_df, qty_df, unit_by_usage_type).
    """
    end = end or date.today()
    start = end - timedelta(days=days)

    ce = _client(cfg)
    pages = _paginate(
        ce,
        TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
        Granularity="DAILY",
        Metrics=["UnblendedCost", "UsageQuantity"],
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": [service]}},
    )

    cost_rows = []
    qty_rows = []
    unit_by_ut: dict[str, str] = {}
    for page in pages:
        for day in page["ResultsByTime"]:
            d = day["TimePeriod"]["Start"]
            for grp in day["Groups"]:
                ut = grp["Keys"][0]
                cost = float(grp["Metrics"]["UnblendedCost"]["Amount"])
                qty = float(grp["Metrics"]["UsageQuantity"]["Amount"])
                unit = grp["Metrics"]["UsageQuantity"].get("Unit", "")
                cost_rows.append((d, ut, cost))
                qty_rows.append((d, ut, qty))
                unit_by_ut.setdefault(ut, unit)

    if not cost_rows:
        return pd.DataFrame(), pd.DataFrame(), {}

    cost_df = pd.DataFrame(cost_rows, columns=["date", "usage_type", "cost"]).pivot_table(
        index="date", columns="usage_type", values="cost", aggfunc="sum"
    ).fillna(0.0)
    cost_df.index = pd.to_datetime(cost_df.index).date
    qty_df = pd.DataFrame(qty_rows, columns=["date", "usage_type", "qty"]).pivot_table(
        index="date", columns="usage_type", values="qty", aggfunc="sum"
    ).fillna(0.0)
    qty_df.index = pd.to_datetime(qty_df.index).date
    return cost_df.sort_index(), qty_df.sort_index(), unit_by_ut


def load_csv(path: str) -> dict[str, pd.DataFrame]:
    """Backtest helper: read a Cost Explorer CSV export into the same shape as fetch_by_service."""
    raw = pd.read_csv(path)
    raw = raw[raw["Service"].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)].copy()
    raw["date"] = pd.to_datetime(raw["Service"]).dt.date
    raw = raw.drop(columns=["Service"]).set_index("date")
    raw.columns = [c.replace("($)", "").strip() for c in raw.columns]
    if "Total costs" in raw.columns:
        raw = raw.rename(columns={"Total costs": "Total"})
    return {_SCOPE: raw.apply(pd.to_numeric, errors="coerce").fillna(0.0).sort_index()}
