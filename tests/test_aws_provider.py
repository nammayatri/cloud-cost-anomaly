import datetime as dt

from providers import aws


_PAGE = {
    "ResultsByTime": [{
        "TimePeriod": {"Start": "2026-09-16"},
        "Groups": [
            {"Keys": ["Amazon Elastic Compute Cloud - Compute"],
             "Metrics": {"UnblendedCost": {"Amount": "400.0"},
                         "NetUnblendedCost": {"Amount": "220.0"}}},
            {"Keys": ["Amazon Relational Database Service"],
             "Metrics": {"UnblendedCost": {"Amount": "100.0"},
                         "NetUnblendedCost": {"Amount": "60.0"}}},
        ],
    }]
}


def test_fetch_by_service_returns_both_metrics(monkeypatch):
    seen = {}

    def fake_paginate(ce, **kwargs):
        seen.update(kwargs)
        return [_PAGE]

    monkeypatch.setattr(aws, "_paginate", fake_paginate)
    monkeypatch.setattr(aws, "_client", lambda cfg, acct: object())
    cfg = {"aws_accounts": [{"label": "Prod"}], "lookback_days": 7}

    out = aws.fetch_by_service(cfg, end=dt.date(2026, 9, 17))
    day = dt.date(2026, 9, 16)

    assert seen["Metrics"] == ["UnblendedCost", "NetUnblendedCost"]
    assert out["Prod"]["usage"].at[day, "Total"] == 500.0
    assert out["Prod"]["invoiced"].at[day, "Total"] == 280.0
