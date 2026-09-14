"""The bookend briefs.

These are the only messages whose arrival is predictable, which is what earns
the right to interrupt at other times — so the bar is that every one is worth
opening. Mostly that means knowing when to say nothing.
"""

import pytest

from app.compose.guard import check
from app.compose.voice import is_compliant
from app.tools.pnl import HoldingPnl, MarginUtilisation, PortfolioPnl, PositionPnl
from app.watcher.briefs import BriefData, Expiring, post_close, pre_market
from app.watcher.exposure import build_book

HIST = [
    1200.0,
    -2400.0,
    600.0,
    -900.0,
    1800.0,
    -400.0,
    1100.0,
    -1400.0,
    500.0,
    -700.0,
    2300.0,
    -1700.0,
]


def h(sym="TITAN", value=123600.0, day=-4080.0):
    return HoldingPnl(
        sym, 40, 2618, value / 40, value, 104720.0, value - 104720.0, 18.0, day, -3.2, 0, 0
    )


def data(
    holdings=None, positions=None, margin=None, expiring=None, suppressed=0, macro="", history=HIST
):
    holdings = [h()] if holdings is None else holdings
    positions = positions or []
    pf = PortfolioPnl(
        sum(x.current_value for x in holdings),
        sum(x.cost for x in holdings),
        0,
        0,
        sum(x.day_change for x in holdings),
        -1.6,
        list(holdings),
    )
    return BriefData(
        book=build_book(pf, positions, daily_pnl_history=history),
        portfolio=pf,
        positions=positions,
        margin=margin,
        expiring=expiring or [],
        suppressed=suppressed,
        macro=macro,
    )


SHORT_CE = PositionPnl("NIFTY25SEP25000CE", "FNO", -75, 182.0, 242.0, -4500, 0, -4500)
MARGIN = MarginUtilisation(0.68, 840000, 326000, 1166000, 610000, 230000, 0, 0, 0)


# ---- knowing when to say nothing ------------------------------------


@pytest.mark.parametrize("render", [pre_market, post_close])
def test_an_empty_book_produces_no_brief(render):
    """A brief for someone holding nothing teaches them to skim."""
    body, _ = render(data(holdings=[]))
    assert body == ""


@pytest.mark.parametrize("render", [pre_market, post_close])
def test_a_missing_section_is_omitted_not_zeroed(render):
    body, _ = render(data(margin=None))
    assert "Margin" not in body and "0%" not in body


def test_macro_appears_only_when_supplied():
    """The signal engine may not have it; the book-only brief still reads."""
    assert "Nifty fut" not in pre_market(data())[0]
    assert "Nifty fut" in pre_market(data(macro="Nifty fut 24,918"))[0]


# ---- what the reader actually gets ----------------------------------


def test_the_pre_market_brief_leads_with_the_book():
    body, _ = pre_market(data(margin=MARGIN))
    assert body.startswith("Good morning")
    assert "Book ₹1,23,600" in body
    assert "Margin 68% used" in body


def test_book_value_excludes_short_legs():
    """A short option is an obligation, not something you own. Counting its
    notional as book value overstates what the reader has."""
    body, _ = pre_market(data(positions=[SHORT_CE]))
    assert "₹1,23,600" in body  # holdings only
    assert "₹1,41,750" not in body  # holdings + short notional


def test_an_expiring_short_is_called_out_with_the_distance_to_strike():
    body, _ = pre_market(data(expiring=[Expiring("NIFTY25SEP25000CE", 0, 25000.0, 24918.0)]))
    assert "expires today" in body
    assert "0.3% under" in body


def test_spot_through_the_strike_reads_as_through():
    body, _ = pre_market(data(expiring=[Expiring("N25000CE", 0, 25000.0, 25180.0)]))
    assert "through" in body


def test_the_wrap_attributes_the_day_to_names():
    holdings = [h("TITAN", day=-4080.0), h("INFY", day=-3900.0), h("HDFCBANK", day=1240.0)]
    body, _ = post_close(data(holdings=holdings))
    assert "Cost you" in body and "TITAN" in body
    assert "Made you" in body and "HDFCBANK" in body


def test_the_day_is_measured_in_the_readers_own_sigmas():
    body, _ = post_close(data(holdings=[h(day=-9000.0)]))
    assert "your usual day" in body


def test_an_ordinary_day_is_not_dressed_up_as_a_big_one():
    body, _ = post_close(data(holdings=[h(day=-200.0)]))
    assert "usual day" not in body


def test_no_sigma_history_means_no_sigma_claim():
    """Never fall back to a constant — that is a guess dressed as a threshold."""
    body, _ = post_close(data(holdings=[h(day=-9000.0)], history=None))
    assert "usual day" not in body


def test_the_wrap_says_how_much_it_held_back():
    """Silence was a decision, not a failure — and if they routinely ask to see
    the held-back items, the gate is too tight."""
    body, _ = post_close(data(suppressed=6))
    assert "Held back 6 things" in body and "show" in body


def test_nothing_held_back_means_no_such_line():
    assert "Held back" not in post_close(data(suppressed=0))[0]


def test_one_held_back_item_is_singular():
    assert "Held back 1 thing " in post_close(data(suppressed=1))[0]


def test_the_wrap_drops_what_already_settled():
    """After the close, an expiry that has passed is history; what matters is
    what you are still holding tomorrow morning."""
    settled = Expiring("GONE25000CE", 0, 25000.0, 24918.0)
    carries = Expiring("CARRY25000CE", 1, 25000.0, 24918.0)
    body, _ = post_close(data(expiring=[settled, carries]))
    assert "GONE25000CE" not in body
    assert "CARRY25000CE" in body


def test_the_pre_market_brief_keeps_todays_expiry():
    body, _ = pre_market(data(expiring=[Expiring("N25000CE", 0, 25000.0, 24918.0)]))
    assert "N25000CE" in body


# ---- the same two gates every outbound message passes ---------------


@pytest.mark.parametrize("render", [pre_market, post_close])
def test_every_number_traces_to_a_slot(render):
    """The ship-blocker, applied to briefs as to alerts."""
    d = data(
        holdings=[h("TITAN", day=-4080.0), h("HDFCBANK", 91200.0, 1240.0)],
        positions=[SHORT_CE],
        margin=MARGIN,
        suppressed=6,
        expiring=[Expiring("NIFTY25SEP25000CE", 1, 25000.0, 24918.0)],
    )
    body, slots = render(d)
    result = check(body, slots)
    assert result.ok, f"untraceable: {result.untraceable}"


@pytest.mark.parametrize("render", [pre_market, post_close])
def test_no_brief_reads_as_advice(render):
    d = data(margin=MARGIN, expiring=[Expiring("N25000CE", 1, 25000.0, 24918.0)])
    assert is_compliant(render(d)[0])
