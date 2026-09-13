"""P&L is the invariant-critical layer: every number the bot shows comes from
here. Spec §9.2 makes numeric exactness a ship-blocker, so these assert exact
values, not approximations.
"""

import pytest

from app.tools.pnl import (
    Basis,
    holding_pnl,
    margin_utilisation,
    open_basis,
    portfolio_pnl,
    position_pnl,
)
from app.tools.types import FnoMargin, Holding, MarginState, Position


def h(**kw):
    base = dict(trading_symbol="RELIANCE", quantity=10, average_price=1000.0)
    return Holding(**{**base, **kw})


def p(**kw):
    base = dict(trading_symbol="NIFTY26SEPFUT", segment="FNO")
    return Position(**{**base, **kw})


# ---- holdings ------------------------------------------------------


def test_holding_value_and_unrealised():
    r = holding_pnl(h(quantity=10, average_price=1000.0), ltp=1100.0, day_change=50.0)
    assert r.current_value == 11_000.0
    assert r.cost == 10_000.0
    assert r.unrealised == 1_000.0
    assert r.unrealised_pct == 10.0


def test_day_change_multiplies_by_quantity():
    """quote.day_change is per unit — Groww does not do the multiply."""
    r = holding_pnl(h(quantity=10), ltp=1100.0, day_change=50.0)
    assert r.day_change == 500.0
    # previous close was 1050, so 10 units moved 10500 -> 11000
    assert r.day_change_pct == pytest.approx(4.7619, rel=1e-4)


def test_loss_is_negative():
    r = holding_pnl(h(quantity=10, average_price=1000.0), ltp=900.0, day_change=-20.0)
    assert r.unrealised == -1_000.0
    assert r.unrealised_pct == -10.0
    assert r.day_change == -200.0


def test_zero_cost_does_not_divide_by_zero():
    r = holding_pnl(h(quantity=10, average_price=0.0), ltp=100.0, day_change=0.0)
    assert r.unrealised_pct == 0.0


def test_pledged_quantity_still_counts_toward_value():
    """Pledged stock is not freely sellable but it is still yours (§5.3 gotcha 3)."""
    r = holding_pnl(h(quantity=12, pledge_quantity=4), ltp=100.0, day_change=0.0)
    assert r.current_value == 1_200.0
    assert r.pledged == 4
    assert r.is_pledged


def test_t1_is_surfaced_separately():
    r = holding_pnl(h(quantity=10, t1_quantity=3), ltp=100.0, day_change=0.0)
    assert r.t1 == 3


# ---- positions -----------------------------------------------------


def test_net_long_uses_credit_price_as_basis():
    pos = p(credit_quantity=150, credit_price=100.0, debit_quantity=75, debit_price=110.0)
    r = position_pnl(pos, ltp=120.0)
    assert r.net_qty == 75
    assert r.basis == 100.0
    assert r.unrealised == 75 * 20.0
    assert r.direction == "long"


def test_net_short_uses_debit_price_and_inverts():
    pos = p(credit_quantity=0, debit_quantity=75, debit_price=200.0)
    r = position_pnl(pos, ltp=150.0)
    assert r.net_qty == -75
    assert r.basis == 200.0
    assert r.unrealised == 75 * 50.0  # sold at 200, now 150 -> profit
    assert r.direction == "short"


def test_short_loses_when_price_rises():
    pos = p(credit_quantity=0, debit_quantity=75, debit_price=200.0)
    assert position_pnl(pos, ltp=250.0).unrealised == -3_750.0


def test_flat_position_has_no_unrealised():
    pos = p(credit_quantity=75, credit_price=100.0, debit_quantity=75, debit_price=110.0)
    r = position_pnl(pos, ltp=999.0)
    assert (r.net_qty, r.unrealised, r.direction) == (0, 0.0, "flat")


def test_realised_is_taken_from_groww_not_recomputed():
    pos = p(credit_quantity=75, credit_price=100.0, realised_pnl=1_234.0)
    r = position_pnl(pos, ltp=110.0)
    assert r.realised == 1_234.0
    assert r.total == r.unrealised + 1_234.0


def test_notional_basis_convention_divides_by_quantity():
    """Appendix B #4: if credit_price turns out to be leg notional, not an
    average, the same payload must produce a different basis."""
    pos = p(credit_quantity=75, credit_price=7_500.0)
    assert open_basis(pos, Basis.AVERAGE) == (75, 7_500.0)
    assert open_basis(pos, Basis.NOTIONAL) == (75, 100.0)


def test_fno_quantities_are_units_not_lots():
    """A NIFTY contract reports qty 75 (units), so P&L must not re-multiply
    by lot size (§5.3 gotcha 2)."""
    pos = p(credit_quantity=75, credit_price=100.0)
    assert position_pnl(pos, ltp=101.0).unrealised == 75.0


# ---- portfolio -----------------------------------------------------


def test_portfolio_aggregates():
    holdings = [
        h(trading_symbol="A", quantity=10, average_price=100.0),
        h(trading_symbol="B", quantity=5, average_price=200.0),
    ]
    r = portfolio_pnl(holdings, {"A": 110.0, "B": 190.0}, {"A": 5.0, "B": -10.0})
    assert r.current_value == 1_100.0 + 950.0
    assert r.cost == 1_000.0 + 1_000.0
    assert r.unrealised == 50.0
    assert r.day_change == 50.0 - 50.0


def test_missing_quote_is_named_not_zeroed():
    """Invariant I5 — staleness is worse than silence, and a zero would be a lie."""
    holdings = [h(trading_symbol="A", quantity=10), h(trading_symbol="GHOST", quantity=5)]
    r = portfolio_pnl(holdings, {"A": 100.0}, {"A": 0.0})
    assert r.missing_quotes == ["GHOST"]
    assert len(r.holdings) == 1
    assert r.current_value == 1_000.0


def test_movers_sorted_by_absolute_day_change():
    holdings = [
        h(trading_symbol="SMALL", quantity=1),
        h(trading_symbol="BIGDOWN", quantity=100),
        h(trading_symbol="BIGUP", quantity=50),
    ]
    ltps = dict.fromkeys(["SMALL", "BIGDOWN", "BIGUP"], 100.0)
    r = portfolio_pnl(holdings, ltps, {"SMALL": 1.0, "BIGDOWN": -10.0, "BIGUP": 5.0})
    assert [m.symbol for m in r.movers] == ["BIGDOWN", "BIGUP", "SMALL"]


# ---- margin --------------------------------------------------------


def test_margin_utilisation():
    m = MarginState(
        clear_cash=100_000.0,
        collateral_available=300_000.0,
        net_margin_used=600_000.0,
        fno=FnoMargin(span_margin_used=412_000.0, exposure_margin_used=88_000.0),
    )
    u = margin_utilisation(m)
    assert u.utilisation == 0.6  # 600k / (600k + 400k)
    assert u.headroom == 400_000.0
    assert u.span == 412_000.0
    assert u.exposure == 88_000.0


def test_margin_with_no_capital_does_not_divide_by_zero():
    assert margin_utilisation(MarginState()).utilisation == 0.0
