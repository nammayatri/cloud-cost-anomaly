import datetime as dt

import budgets
import store

CFG = {
    "cost_ch_host": "h", "cost_ch_user": "u", "cost_ch_password": "p",
    "vendor_type": "Data and Tools", "vendor_cost_head": "Hyperverge",
}
DAY = dt.date(2026, 9, 18)


def _row(type_, head, account, amount):
    return {"type": type_, "cost_head": head, "account": account, "budget_inr": amount}


def test_cloud_for_inverts_the_taxonomy():
    assert store.cloud_for("Cloud", "AWS Cost") == "AWS"
    assert store.cloud_for("Cloud", "GCP Cost") == "GCP"
    assert store.cloud_for("Maps", "Maps Cost") == "GMP"
    assert store.cloud_for("Data and Tools", "Hyperverge") is None   # vendor is config, not taxonomy


def test_resolve_maps_rows_to_cloud_keys():
    rows = [
        _row("Cloud", "AWS Cost", "", 1000000),
        _row("Cloud", "GCP Cost", "", 3000000),
        _row("Maps", "Maps Cost", "", 1500000),
        _row("Data and Tools", "Hyperverge", "", 300000),
    ]
    assert budgets.resolve(rows, CFG) == {
        "AWS": 1000000.0, "GCP": 3000000.0, "GMP": 1500000.0, "VENDOR": 300000.0,
    }


def test_account_rows_sum_when_there_is_no_cost_head_row():
    rows = [_row("Cloud", "AWS Cost", "Beckn Prod", 700000),
            _row("Cloud", "AWS Cost", "Triffy", 250000)]
    assert budgets.resolve(rows, CFG) == {"AWS": 950000.0}


def test_cost_head_row_wins_over_account_rows():
    # The finer rows exist for the dashboard's breakdown; the report wants the
    # single number finance signed off on for the whole head.
    rows = [_row("Cloud", "AWS Cost", "", 1000000),
            _row("Cloud", "AWS Cost", "Beckn Prod", 700000)]
    assert budgets.resolve(rows, CFG) == {"AWS": 1000000.0}


def test_unmapped_taxonomy_is_ignored_not_fatal():
    rows = [_row("Cloud", "AWS Cost", "", 1000000),
            _row("Something", "New Thing", "", 5)]
    assert budgets.resolve(rows, CFG) == {"AWS": 1000000.0}


def test_fetch_is_a_noop_when_the_store_is_not_configured():
    assert budgets.fetch({"vendor_cost_head": "Hyperverge"}, DAY) == {}


def test_fetch_queries_the_first_of_the_month(monkeypatch):
    seen = {}

    def fake_rows(cfg, month):
        seen["month"] = month
        return [_row("Cloud", "GCP Cost", "", 3000000)]

    monkeypatch.setattr(budgets, "_rows", fake_rows)
    assert budgets.fetch(CFG, DAY) == {"GCP": 3000000.0}
    assert seen["month"] == dt.date(2026, 9, 1)


def test_fetch_returns_empty_when_clickhouse_is_unreachable(monkeypatch):
    # {} means "no opinion" so the caller keeps its configured budgets; a zero
    # budget would render as infinitely over plan.
    def boom(cfg, month):
        raise OSError("connection refused")

    monkeypatch.setattr(budgets, "_rows", boom)
    assert budgets.fetch(CFG, DAY) == {}
