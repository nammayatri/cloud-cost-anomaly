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
