import datetime as dt
from pathlib import Path

import collect
import workbook
from tests.factories import frames

CFG = {"report_currency": "INR", "currency": "INR", "usd_inr_rate": 96.0,
       "monthly_budgets": {}, "projection_days": 30}
TARGET = dt.date(2026, 9, 16)
PREV = dt.date(2026, 9, 15)


def _report(invoiced_today):
    f = frames(
        usage={PREV: {"Compute Engine": 80.0}, TARGET: {"Compute Engine": 100.0}},
        invoiced={PREV: {"Compute Engine": 80.0}, TARGET: {"Compute Engine": invoiced_today}},
    )
    s = collect._build_section(CFG, "GCP ny-prod", "GCP", "INR", f, TARGET)
    return {"date": TARGET, "sections": [s], "gmp_apis": None, "rides": None,
            "totals": collect._totals(CFG, [s])}


def test_workbook_builds_with_and_without_credits(tmp_path: Path):
    for invoiced in (0.0, 100.0):
        path = str(tmp_path / f"out-{invoiced}.xlsx")
        workbook.build(path, CFG, _report(invoiced))
        assert Path(path).stat().st_size > 0


def test_summary_and_detail_sheets_show_credits(tmp_path: Path):
    import zipfile

    path = str(tmp_path / "with-credits.xlsx")
    workbook.build(path, CFG, _report(0.0))
    with zipfile.ZipFile(path) as z:
        shared = z.read("xl/sharedStrings.xml").decode()
    assert "Invoiced (INR)" in shared
    assert "credits applied" in shared


def test_no_invoiced_column_without_credits(tmp_path: Path):
    import zipfile

    path = str(tmp_path / "no-credits.xlsx")
    workbook.build(path, CFG, _report(100.0))
    with zipfile.ZipFile(path) as z:
        shared = z.read("xl/sharedStrings.xml").decode()
    assert "Invoiced (INR)" not in shared
    assert "credits applied" not in shared
