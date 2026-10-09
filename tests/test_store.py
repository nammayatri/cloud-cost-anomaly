import datetime as dt

import store

DAY = dt.date(2026, 9, 16)


def test_row_carries_both_bases():
    r = store._row(DAY, "GCP", "GCP ny-prod", "Compute Engine", 100.0, "INR", 1.0,
                   cost_invoiced_native=0.0)
    assert r["cost_native"] == 100.0
    assert r["cost_native_invoiced"] == 0.0
    assert r["cost_inr_invoiced"] == 0.0
    assert r["account"] == "ny-prod"


def test_row_defaults_invoiced_to_usage_when_not_given():
    r = store._row(DAY, "VENDOR", "Vendor", "Module", 40.0, "INR", 1.0)
    assert r["cost_native_invoiced"] == 40.0


def test_exotel_stored_day_reads_and_shapes_clickhouse_rows(monkeypatch):
    cfg = {"cost_ch_host": "h", "cost_ch_user": "u", "cost_ch_password": "p",
           "exotel_cost_head": "Exotel", "exotel_type": "Data and Tools",
           "exotel_account_label": "Exotel"}

    def fake_urlopen(req, timeout=None):
        class _R:
            def read(self):
                return (b'{"service":"inbound","cost":"100.5","units":"10"}\n'
                        b'{"service":"outbound-api","cost":"2.0","units":"1"}\n')
        return _R()

    monkeypatch.setattr(store.urllib.request, "urlopen", fake_urlopen)
    rows = store.exotel_stored_day(cfg, DAY)
    assert rows == [
        {"account": "Exotel", "service": "inbound", "cost": 100.5, "units": 10.0},
        {"account": "Exotel", "service": "outbound-api", "cost": 2.0, "units": 1.0},
    ]


def test_exotel_stored_day_returns_none_on_failure_not_empty(monkeypatch):
    # None means "we don't know" (ClickHouse unreachable); [] means "no rows
    # for that day". _vendor_window_section treats them differently, so this
    # function must not collapse a real failure into a silent zero.
    cfg = {"cost_ch_host": "h", "cost_ch_user": "u", "cost_ch_password": "p"}

    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(store.urllib.request, "urlopen", boom)
    assert store.exotel_stored_day(cfg, DAY) is None


def test_exotel_stored_day_is_none_when_store_not_configured():
    assert store.exotel_stored_day({}, DAY) is None


def test_insert_drops_columns_the_table_does_not_have(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = req.data.decode()

        class _R:
            def read(self):
                return b""
        return _R()

    monkeypatch.setattr(store.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(store, "_table_columns",
                        lambda cfg: {"date", "type", "cost_head", "account", "service",
                                     "cost_native", "currency", "fx_rate", "cost_inr", "units"})
    cfg = {"cost_ch_host": "h", "cost_ch_user": "u", "cost_ch_password": "p"}

    store._insert(cfg, [store._row(DAY, "GCP", "GCP ny-prod", "Compute Engine",
                                   100.0, "INR", 1.0, cost_invoiced_native=0.0)])

    assert "cost_native_invoiced" not in sent["body"]
    assert "cost_native" in sent["body"]
