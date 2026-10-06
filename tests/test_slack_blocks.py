import datetime as dt
import json
from unittest import mock

import collect
import slack
from tests.factories import frames

CFG = {"report_currency": "INR", "currency": "INR", "usd_inr_rate": 96.0,
       "monthly_budgets": {}, "projection_days": 30, "mention": ""}
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


def _text(blocks):
    return json.dumps(blocks)


def test_root_and_split_show_invoiced_when_credits_exist():
    report = _report(invoiced_today=0.0)
    _, attachments = slack.build_root_blocks(CFG, report)
    assert "Invoiced" in _text(attachments[0]["blocks"])
    split = slack._account_split_blocks(CFG, report, {"GCP"}, "*Cloud*")
    assert "Invoiced" in _text(split)


def test_no_invoiced_column_when_there_are_no_credits():
    report = _report(invoiced_today=100.0)
    _, attachments = slack.build_root_blocks(CFG, report)
    assert "Invoiced" not in _text(attachments[0]["blocks"])
    split = slack._account_split_blocks(CFG, report, {"GCP"}, "*Cloud*")
    assert "Invoiced" not in _text(split)


def test_service_table_gets_a_credits_footer_only_with_credits():
    with_credits = _report(invoiced_today=0.0)
    blocks = slack._section_table_blocks(CFG, with_credits, with_credits["sections"][0])
    assert "credits applied" in _text(blocks)

    without = _report(invoiced_today=100.0)
    blocks = slack._section_table_blocks(CFG, without, without["sections"][0])
    assert "credits applied" not in _text(blocks)


def test_post_skips_without_touching_slack_when_disabled():
    with mock.patch.object(slack, "WebClient") as mock_client:
        slack.post({**CFG, "slack_enabled": False}, _report(0.0), "/tmp/wb.xlsx")
    assert not mock_client.called


def test_post_proceeds_when_enabled_by_default():
    cfg = {**CFG, "slack_bot_token": "x", "slack_channel_id": "C1"}
    with mock.patch.object(slack, "WebClient") as mock_client:
        try:
            slack.post(cfg, _report(0.0), "/tmp/wb.xlsx")
        except Exception:
            pass   # we only care whether it got as far as constructing the client
    assert mock_client.called
