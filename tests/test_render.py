from datetime import datetime, timedelta

import pytest

from app.config import IST
from app.render.templates import arrow, as_of, inr, pct, qty, signed


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "₹0"),
        (999, "₹999"),
        (1000, "₹1,000"),
        (99999, "₹99,999"),
        (100000, "₹1,00,000"),
        (124580, "₹1,24,580"),
        (1842600, "₹18,42,600"),
        (10000000, "₹1,00,00,000"),
        (123456789, "₹12,34,56,789"),
    ],
)
def test_indian_digit_grouping(value, expected):
    assert inr(value) == expected


def test_negative_uses_minus_sign_not_hyphen():
    out = inr(-2100)
    assert out == "−₹2,100"
    assert "-" not in out  # U+2212, so a plain hyphen must not appear


def test_decimals():
    assert inr(1234.56, 2) == "₹1,234.56"
    assert inr(1234.5, 1) == "₹1,234.5"


def test_signed_always_carries_a_sign():
    assert signed(24180) == "+₹24,180"
    assert signed(-2100) == "−₹2,100"
    assert signed(0) == "+₹0"


def test_pct_and_arrow():
    assert pct(1.334, 2) == "+1.33%"
    assert pct(-0.9) == "-0.9%"
    assert (arrow(1), arrow(-1), arrow(0)) == ("▲", "▼", "·")


def test_qty_drops_trailing_zeros():
    assert qty(75.0) == "75"
    assert qty(0.5) == "0.5"


def test_as_of_marks_stale_data():
    fresh = datetime.now(IST)
    assert "live" in as_of(fresh)
    stale = datetime.now(IST) - timedelta(minutes=5)
    assert "as of" in as_of(stale)


def test_as_of_includes_scope():
    assert "11 holdings" in as_of(datetime.now(IST), scope="11 holdings")
