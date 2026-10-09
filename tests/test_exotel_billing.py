import datetime as dt
import urllib.error
from unittest import mock

import exotel_billing as eb

CFG = {
    "exotel_api_url": "https://api.exotel.com",
    "exotel_account_sid": "SID1", "exotel_api_key": "KEY", "exotel_api_token": "TOK",
    "exotel_account_label": "Exotel",
}
DAY = dt.date(2026, 10, 1)


def test_configured_requires_url_and_all_three_credentials():
    assert eb.configured(CFG)
    assert not eb.configured({**CFG, "exotel_api_url": None})
    assert not eb.configured({**CFG, "exotel_api_token": None})
    assert not eb.configured({})


def test_fetch_window_sums_price_grouped_by_direction():
    page = {
        "Calls": [
            {"Direction": "outbound-dial", "Price": "1.5"},
            {"Direction": "outbound-dial", "Price": "1.2"},
            {"Direction": "inbound", "Price": "0.5"},
        ],
        "Metadata": {},
    }
    with mock.patch.object(eb, "_get", return_value=page):
        totals = eb._fetch_window(CFG, "SID1", "2026-10-01 00:00:00", "2026-10-01 00:59:59")
    assert totals["outbound-dial"] == {"cost": 2.7, "units": 2}
    assert totals["inbound"] == {"cost": 0.5, "units": 1}


def test_fetch_window_follows_pagination_cursor():
    pages = [
        {"Calls": [{"Direction": "inbound", "Price": "1.0"}],
         "Metadata": {"NextPageUri": "/v1/Accounts/SID1/Calls.json?After=abc"}},
        {"Calls": [{"Direction": "inbound", "Price": "2.0"}], "Metadata": {}},
    ]
    with mock.patch.object(eb, "_get", side_effect=lambda cfg, url: pages.pop(0)):
        totals = eb._fetch_window(CFG, "SID1", "2026-10-01 00:00:00", "2026-10-01 00:59:59")
    assert totals == {"inbound": {"cost": 3.0, "units": 2}}


def test_fetch_window_returns_empty_for_a_real_zero_call_window():
    with mock.patch.object(eb, "_get", return_value={"Calls": [], "Metadata": {}}):
        assert eb._fetch_window(CFG, "SID1", "2026-10-01 00:00:00", "2026-10-01 00:59:59") == {}


def test_fetch_day_merges_all_24_hourly_windows_by_default():
    with mock.patch.object(eb, "_fetch_window", return_value={"inbound": {"cost": 1.0, "units": 1}}):
        rows = eb.fetch_day(CFG, DAY)
    assert rows == [{"account": "Exotel", "service": "inbound", "cost": 24.0, "units": 24.0}]


def test_fetch_day_respects_configured_window_size():
    # 6-hour windows -> 4 windows/day, not 24. Confirms the split comes from
    # config (exotel_window_minutes), not a hardcoded 24.
    cfg = {**CFG, "exotel_window_minutes": 360}
    with mock.patch.object(eb, "_fetch_window", return_value={"inbound": {"cost": 1.0, "units": 1}}) as m:
        rows = eb.fetch_day(cfg, DAY)
    assert m.call_count == 4
    assert rows == [{"account": "Exotel", "service": "inbound", "cost": 4.0, "units": 4.0}]


def test_day_windows_covers_the_day_with_no_gaps_or_overlaps():
    windows = eb._day_windows(DAY, 60)
    assert len(windows) == 24
    assert windows[0] == ("2026-10-01 00:00:00", "2026-10-01 00:59:59")
    assert windows[-1] == ("2026-10-01 23:00:00", "2026-10-01 23:59:59")


def test_day_windows_handles_a_non_dividing_window_size():
    # 1000-minute windows don't divide evenly into a day -> 2 windows, the
    # second shorter, still ending exactly at day's end.
    windows = eb._day_windows(DAY, 1000)
    assert len(windows) == 2
    assert windows[-1][1] == "2026-10-01 23:59:59"


def test_fetch_window_uses_configured_page_size_and_api_url():
    captured = {}

    def fake_get(cfg, url):
        captured["url"] = url
        return {"Calls": [], "Metadata": {}}

    cfg = {**CFG, "exotel_api_url": "https://custom.exotel.example/", "exotel_page_size": 50}
    with mock.patch.object(eb, "_get", side_effect=fake_get):
        eb._fetch_window(cfg, "SID1", "2026-10-01 00:00:00", "2026-10-01 00:59:59")
    assert captured["url"].startswith("https://custom.exotel.example/v1/Accounts/SID1/Calls.json")
    assert "PageSize=50" in captured["url"]


def test_fetch_day_raises_if_any_window_fails():
    with mock.patch.object(eb, "_fetch_window", side_effect=OSError("connection refused")):
        try:
            eb.fetch_day(CFG, DAY)
            assert False, "expected fetch_day to raise"
        except OSError:
            pass


def test_get_retries_on_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(eb.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        import io
        return io.BytesIO(b'{"Calls": [], "Metadata": {}}')

    with mock.patch.object(eb.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = eb._get(CFG, "https://api.exotel.com/v1/Accounts/SID1/Calls.json")
    assert result == {"Calls": [], "Metadata": {}}
    assert calls["n"] == 3


def test_get_does_not_retry_non_429_errors(monkeypatch):
    monkeypatch.setattr(eb.time, "sleep", lambda s: None)

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with mock.patch.object(eb.urllib.request, "urlopen", side_effect=fake_urlopen):
        try:
            eb._get(CFG, "https://api.exotel.com/v1/Accounts/SID1/Calls.json")
            assert False, "expected _get to raise on 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
