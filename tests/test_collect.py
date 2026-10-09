import datetime as dt
from unittest import mock

import collect
import exotel_billing
import store
from tests.factories import frames

CFG = {"report_currency": "INR", "currency": "INR", "usd_inr_rate": 96.0}
TARGET = dt.date(2026, 9, 16)
PREV = dt.date(2026, 9, 15)


def _section(usage_today=100.0, invoiced_today=0.0):
    f = frames(
        usage={PREV: {"Compute Engine": 80.0}, TARGET: {"Compute Engine": usage_today}},
        invoiced={PREV: {"Compute Engine": 80.0}, TARGET: {"Compute Engine": invoiced_today}},
    )
    return collect._build_section(CFG, "GCP ny-prod", "GCP", "INR", f, TARGET)


def test_section_carries_both_bases_and_credits():
    s = _section()
    assert s["total_native"] == 100.0
    assert s["total_invoiced_report"] == 0.0
    assert s["credits_report"] == 100.0
    assert s["rows"][0]["today_invoiced_report"] == 0.0


def test_percentages_use_the_usage_basis_only():
    s = _section()
    assert s["dod_pct"] == 25.0          # 80 -> 100, unaffected by the credit


def test_totals_roll_up_both_bases():
    t = collect._totals(CFG, [_section()])
    assert t["grand_total"] == 100.0
    assert t["grand_total_invoiced"] == 0.0
    assert t["by_cloud_invoiced"]["GCP"] == 0.0
    assert t["credits"] == 100.0
    assert t["buckets"][0]["total_invoiced"] == 0.0


def test_credits_visible_respects_display_rounding():
    assert collect.credits_visible(100.0, 0.0, 0) is True
    assert collect.credits_visible(100.0, 100.0, 0) is False
    assert collect.credits_visible(100.006, 100.0, 0) is False   # rounds away in INR
    assert collect.credits_visible(100.006, 100.0, 2) is True    # survives 2dp


def test_exotel_section_only_live_fetches_the_target_day():
    # Exotel's real call volume makes a live fetch take ~10 minutes even
    # parallelized — only the target day may ever hit the live API; the rest
    # of the WoW window must come from ClickHouse instead (store.exotel_stored_day).
    cfg = {**CFG, "exotel_account_sid": "x", "exotel_api_key": "k", "exotel_api_token": "t",
           "exotel_account_label": "Exotel"}
    live_days, stored_days = [], []

    def fake_live(cfg, d):
        live_days.append(d)
        return [{"account": "Exotel", "service": "inbound", "cost": 100.0, "units": 10.0}]

    def fake_stored(cfg, d):
        stored_days.append(d)
        return [{"account": "Exotel", "service": "inbound", "cost": 50.0, "units": 5.0}]

    with mock.patch.object(store, "exotel_priced_day", side_effect=fake_live), \
         mock.patch.object(store, "exotel_stored_day", side_effect=fake_stored), \
         mock.patch.object(exotel_billing, "configured", return_value=True):
        sections = collect._exotel_section(cfg, TARGET)

    assert live_days == [TARGET]
    assert TARGET not in stored_days
    assert len(stored_days) == collect.WINDOW_DAYS   # the other days in the window
    assert sections and sections[0]["cloud"] == "EXOTEL"
