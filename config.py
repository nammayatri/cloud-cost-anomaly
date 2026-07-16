import json
import os
from pathlib import Path

_DEFAULTS = {
    # Which cloud to report on: "aws" | "gcp"
    "provider": "aws",

    # --- AWS ---
    "aws_profile": None,
    "aws_region": "ap-south-1",

    # --- GCP ---
    # Fully-qualified billing export table: project.dataset.table
    "gcp_billing_table": None,
    # Project that runs/bills the BigQuery job. Defaults to ADC's project.
    "gcp_project": None,
    # REQUIRED for gcp. The export is billing-account-wide, so without this we'd
    # report every project's spend. Order is preserved: first = top of report.
    "gcp_projects": [],

    # --- Reporting ---
    # ISO code. Drives money formatting. AWS reports USD; GCP uses the billing
    # account's currency (INR for us).
    "currency": None,

    "slack_bot_token": None,
    "slack_channel_id": None,
    "increase_pct_threshold": 10.0,
    "decrease_pct_threshold": 5.0,
    # abs_threshold/noise_floor are in the reporting currency. $1 and ₹1 are not
    # the same guard — see _CURRENCY_DEFAULTS below.
    "abs_threshold": None,
    "noise_floor": None,
    "lookback_days": 21,
    "top_usage_types": 3,
    "mention": "",
}

# Sensible per-currency floors, applied only when not set explicitly. Roughly
# equivalent in real terms — ₹1 would fire on virtually every SKU.
_CURRENCY_DEFAULTS = {
    "USD": {"abs_threshold": 1.0, "noise_floor": 1.0},
    "INR": {"abs_threshold": 100.0, "noise_floor": 100.0},
}

_CAST = {
    "increase_pct_threshold": float,
    "decrease_pct_threshold": float,
    "abs_threshold": float,
    "noise_floor": float,
    "lookback_days": int,
    "top_usage_types": int,
}

# Env vars that carry a list, as comma-separated values.
_LIST_KEYS = ("gcp_projects",)


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
        if key in _LIST_KEYS:
            cfg[key] = [v.strip() for v in env_val.split(",") if v.strip()]
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

    if cfg["provider"] not in ("aws", "gcp"):
        raise RuntimeError(f"provider must be 'aws' or 'gcp', got {cfg['provider']!r}")

    if cfg["currency"] is None:
        cfg["currency"] = "USD" if cfg["provider"] == "aws" else "INR"

    # Currency-aware floors, only where the user didn't pin them.
    ccy_defaults = _CURRENCY_DEFAULTS.get(cfg["currency"], _CURRENCY_DEFAULTS["USD"])
    for key, val in ccy_defaults.items():
        if cfg.get(key) is None:
            cfg[key] = val

    missing = [k for k in ("slack_bot_token", "slack_channel_id") if not cfg.get(k)]
    if cfg["provider"] == "gcp":
        if not cfg.get("gcp_billing_table"):
            missing.append("gcp_billing_table")
        if not cfg.get("gcp_projects"):
            missing.append("gcp_projects")
    if missing:
        raise RuntimeError(f"Missing required config keys: {missing}. Set via env or config.json")

    return cfg
