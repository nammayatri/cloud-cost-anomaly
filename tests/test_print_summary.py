import datetime as dt

import collect
import main
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


def test_summary_prints_invoiced_only_when_credits_exist(capsys):
    main._print_summary(CFG, _report(0.0), "/tmp/x.xlsx")
    assert "invoiced" in capsys.readouterr().out

    main._print_summary(CFG, _report(100.0), "/tmp/x.xlsx")
    assert "invoiced" not in capsys.readouterr().out
