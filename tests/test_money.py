import money

CFG = {"report_currency": "INR"}


def test_negative_zero_prints_as_zero():
    # cost + its own offsetting credit lands on -0.0; "-₹0" is nonsense on screen.
    assert money.fmt(CFG, -0.0) == "₹0"
    assert money.fmt(CFG, -0.0004) == "₹0"


def test_real_negatives_keep_their_sign():
    assert money.fmt(CFG, -1234.0) == "-₹1,234"
