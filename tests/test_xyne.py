import datetime as dt
from unittest import mock

import slack
import xyne

CFG = {
    "xyne_base_url": "https://xyne.example", "xyne_jwt": "tok", "xyne_channel": "systems",
    "xyne_mention": "@here", "control_center_dashboard": "https://control-center.moving.tech/analytics/executive-cost",
    "xyne_weekly_only": True, "report_currency": "INR",
}
REPORT = {"date": dt.date(2026, 10, 3)}
MONDAY = dt.date(2026, 10, 5)
TUESDAY = dt.date(2026, 10, 6)


def _run(cfg, today):
    uploads = []

    def fake_upload(cfg, path, thread_ts=None, comment=None, filename=None):
        uploads.append({"path": path, "thread_ts": thread_ts, "comment": comment})
        return {"ts": "123.456"}

    with mock.patch.object(slack, "render_images",
                            return_value=[("summary", "/tmp/a.png"), ("breakdown", "/tmp/b.png")]), \
         mock.patch.object(xyne, "_upload", side_effect=fake_upload), \
         mock.patch.object(xyne, "_post", return_value={"ts": "x"}), \
         mock.patch.object(xyne, "date") as mdate:
        mdate.today.return_value = today
        xyne.post(cfg, REPORT, "/tmp/wb.xlsx")
    return uploads


def test_posts_on_monday_with_dashboard_link():
    uploads = _run(CFG, MONDAY)
    assert len(uploads) == 3   # summary + breakdown + workbook
    assert "<https://control-center.moving.tech/analytics/executive-cost|Dashboard>" in uploads[0]["comment"]


def test_skips_entirely_on_a_non_monday():
    uploads = _run(CFG, TUESDAY)
    assert uploads == []


def test_weekly_only_false_posts_every_day():
    cfg = {**CFG, "xyne_weekly_only": False}
    uploads = _run(cfg, TUESDAY)
    assert len(uploads) == 3


def test_no_dashboard_link_when_unconfigured():
    cfg = {**CFG, "control_center_dashboard": None}
    uploads = _run(cfg, MONDAY)
    assert "Dashboard" not in uploads[0]["comment"]
