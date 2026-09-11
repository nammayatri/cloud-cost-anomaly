import json
import os
from pathlib import Path

_DEFAULTS = {
    # Which cloud to report on: "aws" | "gcp" | "all".
    # "all" builds the combined multi-tab workbook (AWS + GCP + GMP + rides).
    "provider": "all",

    # --- AWS ---
    "aws_profile": None,
    "aws_region": "ap-south-1",
    # Accounts to report on, one tab each. Each entry:
    #   {"label": "AWS Prod", "role_arn": null}   -> use ambient credentials (IRSA)
    #   {"label": "AWS EU",   "role_arn": "arn:aws:iam::<id>:role/<role>"}
    #   {"label": "AWS UAT",  "profile": "uat"}   -> local dev only
    # Cost Explorer is scoped to the credentialed account, so cross-account
    # reporting needs one assume-role per account. If your organisation's payer
    # account is not one you control, consolidated LINKED_ACCOUNT grouping is not
    # available and per-account roles are the only route.
    "aws_accounts": [],

    # --- GCP infra billing export ---
    # Fully-qualified billing export table: project.dataset.table
    "gcp_billing_table": None,
    # Project that runs/bills the BigQuery job. Defaults to ADC's project.
    "gcp_project": None,
    # REQUIRED for gcp. The export is billing-account-wide, so without this we'd
    # report every project's spend. Order is preserved: first = top of report.
    "gcp_projects": [],

    # --- Google Maps Platform billing export ---
    # GMP bills through its own billing account and exports to its own table, but
    # the schema is identical to the infra export — same provider code reads both.
    "gmp_billing_table": None,
    "gmp_projects": [],

    # --- Rides (ClickHouse) ---
    # Used for the cost-per-ride headline. If unset, the ride metrics are omitted
    # from the report rather than failing the run.
    "clickhouse_host": None,
    "clickhouse_port": 8123,
    "clickhouse_user": None,
    "clickhouse_password": None,
    "clickhouse_database": "default",
    "clickhouse_secure": False,
    # Directory of *.sql ride-count queries, one file per ride source. Supplied as
    # a mounted ConfigMap in Kubernetes — the SQL is deliberately not in this repo
    # (public) and the definition of "a ride" belongs to whoever owns the product,
    # not to the cost report. Unset => ride metrics are skipped.
    "ride_query_dir": None,
    # Which ride source is the ride-hailing count (the rest, e.g. tickets, are
    # additive). Defaults to the first query file in filename order.
    "primary_ride_source": None,
    # Monthly budget per cloud, in the reporting currency, keyed by cloud
    # ("AWS", "GCP", "GMP"). The report projects the day's spend to a month and
    # compares. Empty => the budget section is omitted.
    "monthly_budgets": {},
    # Days used for the run-rate projection.
    "projection_days": 30,

    # --- Reporting ---
    # ISO code. Drives money formatting. AWS reports USD; GCP reports whatever
    # currency the billing account is denominated in.
    "currency": None,
    # The workbook rolls every cloud into ONE reporting currency. Cost Explorer
    # only ever returns USD, so AWS totals are converted at this rate. There is no
    # live FX lookup on purpose: a cron that silently changes its numbers because
    # an exchange-rate API moved is worse than one with a rate you control and
    # can see in the manifest.
    "report_currency": "INR",
    # Live USD->INR, fetched for the target day. `usd_inr_rate` is the fallback
    # used when the fetch fails, so the run still completes with a known number.
    "fx_fetch": True,
    "fx_api_url": "https://api.frankfurter.app/{date}?from=USD&to=INR",
    # currencyapi.net key. When set, it is the primary live FX source
    # (frankfurter is the date-specific fallback).
    "fx_currencyapi_key": None,
    "fx_currencyapi_url": "https://currencyapi.net/api/v2/rates",
    "usd_inr_rate": 88.0,

    "slack_bot_token": None,
    "slack_channel_id": None,

    # --- Xyne (Slack-compatible adapter) ---
    # Optional second destination. All three must be set or Xyne is skipped.
    # The channel is a NAME without a leading "#" — "#name" returns channel_not_found.
    "xyne_base_url": None,
    "xyne_jwt": None,
    "xyne_channel": None,
    # Literal mention string prepended nowhere — appended to the Xyne root message.
    # Xyne has its own directory, so Slack user/group IDs do not carry over.
    "xyne_mention": None,

    # --- Cost store (ClickHouse cost_analytics.cost_daily) ---
    # Optional. When set, each run persists the day's per-service cost for the
    # Control Center. Dedicated write-scoped user, separate from ride-count reads.
    "cost_ch_host": None,
    "cost_ch_port": 8123,
    "cost_ch_user": None,
    "cost_ch_password": None,
    "cost_ch_database": "cost_analytics",
    "cost_ch_table": "cost_daily",
    "cost_ch_secure": False,

    # --- Third-party vendor daily cost (optional) ---
    # Cost is computed here, not fetched pre-computed: a machine-credentialed logs
    # API returns one row per request made that day (endpoint + status), and each
    # is priced by `vendor_pricing`. Everything vendor-specific (endpoint,
    # credentials, prices) is supplied here, never hardcoded.
    "vendor_logs_api_url": None,         # POST {"date": "YYYY-MM-DD"} -> presigned CSV url
    "vendor_app_id": None,               # "appid" request header
    "vendor_app_key": None,              # "appKey" request header — secret
    # Price per request, keyed by endpoint path exactly as logged minus any query
    # string (e.g. "/v1/readId"), in report_currency. An endpoint with no entry
    # here still shows up in the report at zero cost — see vendor_billing.py.
    "vendor_pricing": {},
    "vendor_account_label": "Vendor",    # account-column label
    "vendor_type": "Data and Tools",     # `type` grouping for these rows
    "vendor_cost_head": "Vendor",

    "lookback_days": 21,
    "mention": "",
}

_CAST = {
    "usd_inr_rate": float,
    "lookback_days": int,
    "clickhouse_port": int,
    "cost_ch_port": int,
    "projection_days": int,
}

# Env vars that carry a list, as comma-separated values.
_LIST_KEYS = ("gcp_projects", "gmp_projects")

# Env vars that carry structured data, as a JSON string.
_JSON_KEYS = ("aws_accounts", "monthly_budgets", "vendor_pricing")

_BOOL_KEYS = ("clickhouse_secure", "fx_fetch", "cost_ch_secure")

_TRUTHY = {"1", "true", "yes", "on"}


def load(config_path: str | None = None, provider: str | None = None) -> dict:
    cfg = dict(_DEFAULTS)

    path = Path(config_path) if config_path else Path(__file__).parent / "config.json"
    if path.exists():
        with path.open() as f:
            cfg.update(json.load(f))

    for key in cfg:
        env_val = os.environ.get(key.upper())
        if env_val is None or env_val == "":
            continue
        if key in _JSON_KEYS:
            try:
                cfg[key] = json.loads(env_val)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Config key {key!r} (env {key.upper()}) must be valid JSON: {e}"
                ) from None
        elif key in _LIST_KEYS:
            cfg[key] = [v.strip() for v in env_val.split(",") if v.strip()]
        elif key in _BOOL_KEYS:
            cfg[key] = env_val.strip().lower() in _TRUTHY
        else:
            caster = _CAST.get(key, str)
            try:
                cfg[key] = caster(env_val)
            except (ValueError, TypeError) as e:
                raise RuntimeError(
                    f"Config key {key!r} (env {key.upper()}={env_val!r}) is not a valid "
                    f"{caster.__name__}: {e}"
                ) from None

    # Normalize list keys that may arrive from config.json as a plain string
    # ("a,b" instead of ["a","b"]) — otherwise iteration yields characters and
    # silently misconfigures the project scope.
    for key in _LIST_KEYS:
        val = cfg.get(key)
        if isinstance(val, str):
            cfg[key] = [v.strip() for v in val.split(",") if v.strip()]

    # CLI wins over both env and file.
    if provider:
        cfg["provider"] = provider

    if cfg["provider"] not in ("aws", "gcp", "all"):
        raise RuntimeError(f"provider must be 'aws', 'gcp' or 'all', got {cfg['provider']!r}")

    if cfg["currency"] is None:
        cfg["currency"] = "USD" if cfg["provider"] == "aws" else "INR"


    _validate_aws_accounts(cfg)

    missing = [k for k in ("slack_bot_token", "slack_channel_id") if not cfg.get(k)]
    if cfg["provider"] in ("gcp", "all"):
        if not cfg.get("gcp_billing_table"):
            missing.append("gcp_billing_table")
        if not cfg.get("gcp_projects"):
            missing.append("gcp_projects")
    if missing:
        raise RuntimeError(f"Missing required config keys: {missing}. Set via env or config.json")

    return cfg


def _validate_aws_accounts(cfg: dict) -> None:
    """Fail loudly on a malformed account list.

    A typo here is silently expensive: a dropped entry means a whole account
    quietly vanishes from the report and its spend stops being watched, which is
    exactly the failure this tool exists to prevent.
    """
    accounts = cfg.get("aws_accounts") or []
    if not isinstance(accounts, list):
        raise RuntimeError(f"aws_accounts must be a list, got {type(accounts).__name__}")
    seen = set()
    for i, acct in enumerate(accounts):
        if not isinstance(acct, dict):
            raise RuntimeError(f"aws_accounts[{i}] must be an object, got {acct!r}")
        label = acct.get("label")
        if not label:
            raise RuntimeError(f"aws_accounts[{i}] is missing a 'label'")
        if label in seen:
            raise RuntimeError(f"aws_accounts has a duplicate label {label!r} — tab names must be unique")
        seen.add(label)
