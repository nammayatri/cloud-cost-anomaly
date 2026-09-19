import datetime as dt

from tests.factories import frames


def test_frames_builds_both_bases_with_total_column():
    d = dt.date(2026, 9, 16)
    f = frames(
        usage={d: {"Compute Engine": 100.0, "AlloyDB": 25.0}},
        invoiced={d: {"Compute Engine": 0.0, "AlloyDB": 0.0}},
    )
    assert set(f) == {"usage", "invoiced"}
    assert f["usage"].at[d, "Compute Engine"] == 100.0
    assert f["usage"].at[d, "Total"] == 125.0
    assert f["invoiced"].at[d, "Total"] == 0.0


def test_frames_defaults_invoiced_to_usage():
    d = dt.date(2026, 9, 16)
    f = frames(usage={d: {"Vendor Module": 40.0}})
    assert f["invoiced"].at[d, "Vendor Module"] == 40.0
