from providers import gcp


def test_usage_cost_excludes_promotions_and_invoice_rows():
    expr = gcp._USAGE_COST
    assert "c.type != 'PROMOTION'" in expr
    assert "service.description = 'Invoice'" in expr


def test_invoiced_cost_counts_every_credit_and_keeps_invoice_rows():
    expr = gcp._INVOICED_COST
    assert "PROMOTION" not in expr
    assert "Invoice" not in expr


def test_fetch_by_service_pivots_both_bases(monkeypatch):
    import pandas as pd

    captured = {}

    def fake_run(cfg, sql, params):
        captured["sql"] = sql
        return pd.DataFrame([
            {"project": "ny-prod", "service": "Compute Engine",
             "day": pd.Timestamp("2026-09-16").date(), "cost": 100.0, "cost_invoiced": 0.0},
            {"project": "ny-prod", "service": "AlloyDB",
             "day": pd.Timestamp("2026-09-16").date(), "cost": 25.0, "cost_invoiced": 0.0},
        ])

    monkeypatch.setattr(gcp, "_run", fake_run)
    cfg = {"gcp_billing_table": "p.d.t", "gcp_projects": ["ny-prod"], "lookback_days": 7}

    out = gcp.fetch_by_service(cfg)

    assert set(out["ny-prod"]) == {"usage", "invoiced"}
    assert out["ny-prod"]["usage"].at[pd.Timestamp("2026-09-16").date(), "Total"] == 125.0
    assert out["ny-prod"]["invoiced"].at[pd.Timestamp("2026-09-16").date(), "Total"] == 0.0
    assert gcp._USAGE_COST in captured["sql"] and gcp._INVOICED_COST in captured["sql"]
