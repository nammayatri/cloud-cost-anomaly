"""Synthetic provider output for tests.

Mirrors the provider contract exactly: a date-indexed frame per basis, one column
per service plus 'Total'. Tests build cost shapes here so no test needs BigQuery
or Cost Explorer.
"""

import pandas as pd


def _frame(by_day: dict) -> pd.DataFrame:
    services = sorted({s for day in by_day.values() for s in day})
    rows = [{s: by_day[d].get(s, 0.0) for s in services} for d in sorted(by_day)]
    df = pd.DataFrame(rows, index=sorted(by_day))
    df["Total"] = df.sum(axis=1)
    return df


def frames(usage: dict, invoiced: dict | None = None) -> dict[str, pd.DataFrame]:
    """{"usage": df, "invoiced": df}. `invoiced` defaults to `usage` (no credits)."""
    return {"usage": _frame(usage), "invoiced": _frame(invoiced if invoiced is not None else usage)}
