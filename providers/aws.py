"""AWS cost provider — Cost Explorer (ce:GetCostAndUsage), multi-account.

Cost Explorer is scoped to the credentialed account. Consolidated billing would
let an organisation's MANAGEMENT ACCOUNT pull every linked account in one call via
`GroupBy LINKED_ACCOUNT` — but that is only available if you control the payer
account. From a member account, CE returns exactly one account's data.

The workaround is one CE client per account, reaching the others by assuming a
read-only role there. Each account becomes its own scope (its own report tab),
which is the same shape the GCP provider already uses for projects.

Every account's costs come back in USD regardless of where it sits; converting to
the reporting currency is the caller's job (see money.to_report_currency).

Both cost bases are fetched in the same call: UnblendedCost is the usage basis (it
already reflects RI/Savings Plan discounts but no credits), NetUnblendedCost is
what is actually invoiced. Asking for both metrics at once costs nothing extra —
Cost Explorer bills per request, not per metric.
"""

import logging
from datetime import date, timedelta

import boto3
import pandas as pd

log = logging.getLogger("cost-anomaly.aws")

_DEFAULT_LABEL = "AWS"

# One STS assume-role per account per run is plenty; cache so the drill-down
# calls don't re-assume for every flagged service.
_SESSION_CACHE: dict[str, boto3.Session] = {}


def _accounts(cfg: dict) -> list[dict]:
    """Configured accounts, falling back to a single ambient-credential account."""
    accounts = cfg.get("aws_accounts") or []
    if not accounts:
        return [{"label": _DEFAULT_LABEL, "profile": cfg.get("aws_profile") or None}]
    return accounts


def scopes(cfg: dict) -> list[str]:
    return [a["label"] for a in _accounts(cfg)]


def currency(cfg: dict) -> str:
    return "USD"


def _session_for(cfg: dict, acct: dict) -> boto3.Session:
    label = acct["label"]
    if label in _SESSION_CACHE:
        return _SESSION_CACHE[label]

    region = acct.get("region") or cfg.get("aws_region")
    role_arn = acct.get("role_arn")

    if acct.get("access_key_id"):
        # Explicit credentials. Intended for short-lived STS material injected at
        # run time (verification runs, local debugging against several accounts at
        # once). Prefer role_arn in production — a role is auditable and cannot
        # leak into a config file.
        session = boto3.Session(
            aws_access_key_id=acct["access_key_id"],
            aws_secret_access_key=acct["secret_access_key"],
            aws_session_token=acct.get("session_token"),
            region_name=region,
        )
    elif role_arn:
        # Assume into the target account. The base session uses whatever the cron
        # already has (IRSA in-cluster, a profile locally).
        base = boto3.Session(profile_name=cfg.get("aws_profile") or None, region_name=region)
        resp = base.client("sts").assume_role(
            RoleArn=role_arn, RoleSessionName="cost-anomaly-cron"
        )
        c = resp["Credentials"]
        session = boto3.Session(
            aws_access_key_id=c["AccessKeyId"],
            aws_secret_access_key=c["SecretAccessKey"],
            aws_session_token=c["SessionToken"],
            region_name=region,
        )
    else:
        session = boto3.Session(profile_name=acct.get("profile") or cfg.get("aws_profile") or None,
                                region_name=region)

    _SESSION_CACHE[label] = session
    return session


def _client(cfg: dict, acct: dict):
    return _session_for(cfg, acct).client("ce")


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


def _pivot(df, value_col: str):
    """Long rows -> date-indexed frame, one column per service, plus 'Total'."""
    pivot = df.pivot_table(index="date", columns="service", values=value_col,
                           aggfunc="sum").fillna(0.0)
    pivot.index = pd.to_datetime(pivot.index).date
    pivot = pivot.sort_index()
    pivot["Total"] = pivot.sum(axis=1)
    return pivot


def fetch_by_service(cfg: dict, end: date | None = None) -> dict[str, dict]:
    """Daily cost grouped by SERVICE, per account, on both bases (UnblendedCost =
    usage, NetUnblendedCost = invoiced), for the trailing lookback_days."""
    end = end or date.today()
    start = end - timedelta(days=cfg["lookback_days"])

    out: dict[str, dict] = {}
    for acct in _accounts(cfg):
        # One unreachable account must not take the others down with it. A missing
        # cross-account role or an expired credential is a per-account problem;
        # failing the whole fetch would silently drop every AWS tab from the report
        # and make a credentials bug look like "AWS spent nothing today".
        try:
            pages = _paginate(
                _client(cfg, acct),
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity="DAILY",
                Metrics=["UnblendedCost", "NetUnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
        except Exception as e:
            log.error("AWS account %s unavailable — omitting its tab: %s", acct["label"], e)
            continue

        rows = []
        for page in pages:
            for day in page["ResultsByTime"]:
                d = day["TimePeriod"]["Start"]
                for grp in day["Groups"]:
                    svc = grp["Keys"][0]
                    m = grp["Metrics"]
                    rows.append((d, svc,
                                 float(m["UnblendedCost"]["Amount"]),
                                 float(m["NetUnblendedCost"]["Amount"])))

        if not rows:
            continue

        df = pd.DataFrame(rows, columns=["date", "service", "cost", "cost_invoiced"])
        out[acct["label"]] = {
            "usage": _pivot(df, "cost"),
            "invoiced": _pivot(df, "cost_invoiced"),
        }

    return out
