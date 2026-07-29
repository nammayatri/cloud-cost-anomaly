"""Cloud cost providers.

A provider supplies daily cost data in a cloud-agnostic shape so that collect.py
and slack.py never need to know which cloud they're looking at.

Contract
--------
scopes(cfg) -> list[str]
    Ordered list of scope names. A "scope" is the unit a report section is built
    for: the whole account on AWS, one project on GCP. Order is preserved in the
    Slack output, so the first scope is the one that appears at the top.

fetch_by_service(cfg, end) -> dict[scope, DataFrame]
    Per scope: a DataFrame indexed by date, one column per service, plus 'Total'.
    Covers the trailing cfg['lookback_days'], ending at `end` (exclusive).

currency(cfg) -> str
    ISO code the provider reports in. Drives money formatting in slack.py.
"""

from importlib import import_module

_PROVIDERS = ("aws", "gcp")


def get(name: str):
    """Return the provider module for `name` ('aws' or 'gcp')."""
    if name not in _PROVIDERS:
        raise ValueError(f"Unknown provider {name!r}. Expected one of {_PROVIDERS}.")
    return import_module(f"providers.{name}")
