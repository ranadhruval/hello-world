"""One reader of the book for the desk and the briefs.

Pins the two ways the brief's private copy had drifted: F&O positions were
priced off CASH quotes (so every option fell back to entry price and showed
zero P&L), and day changes were never fetched (so attribution lines never
rendered).
"""

import pytest

from app.book import read_book
from app.infra import Cache, CircuitBreaker, MemoryBackend, RateLimiter
from app.tools.groww import GrowwTools
from tests.test_dispatch import FakeBroker, FakeGroww

SHORT_CALL = {
    "trading_symbol": "NIFTY25SEP25000CE",
    "segment": "FNO",
    "exchange": "NSE",
    "credit_quantity": 0,
    "debit_quantity": 75,
    "debit_price": 120.0,
}


class FakeGrowwWithPositions(FakeGroww):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.ltp_segments: list[str] = []

    def get_positions_for_user(self, segment=None, timeout=None):
        self._guard("positions")
        return {"positions": [SHORT_CALL]}

    def get_ltp(self, exchange_trading_symbols=(), segment=None, timeout=None):
        self.ltp_segments.append(segment)
        out = super().get_ltp(exchange_trading_symbols, segment, timeout)
        if segment == "FNO":
            out["NSE_NIFTY25SEP25000CE"] = 80.0
        return out

    def get_available_margin_details(self, timeout=None):
        self._guard("margin")
        raise RuntimeError("margin service down")


def tools_for(client) -> GrowwTools:
    return GrowwTools(
        FakeBroker(client),
        user_id=1,
        cache=Cache(MemoryBackend()),
        limiter=RateLimiter(MemoryBackend()),
        breaker=CircuitBreaker(),
    )


@pytest.mark.asyncio
async def test_positions_are_priced_on_their_own_segment(real_index):
    client = FakeGrowwWithPositions()
    snap = await read_book(tools_for(client), real_index)

    assert "FNO" in client.ltp_segments
    (row,) = snap.positions
    assert row.net_qty == -75
    assert row.ltp == 80.0
    assert row.unrealised == pytest.approx((120.0 - 80.0) * 75)


@pytest.mark.asyncio
async def test_day_changes_reach_the_portfolio(real_index):
    snap = await read_book(tools_for(FakeGroww()), real_index)
    by_symbol = {r.symbol: r for r in snap.portfolio.holdings}
    assert by_symbol["RELIANCE"].day_change == pytest.approx(25.0 * 10)


@pytest.mark.asyncio
async def test_margin_failure_costs_only_the_margin_line(real_index):
    snap = await read_book(tools_for(FakeGrowwWithPositions()), real_index, margin=True)
    assert snap.margin is None
    assert snap.positions and snap.portfolio.holdings


# ---- a book that spans two segments ---------------------------------

CRUDE_PUT = {
    "trading_symbol": "CRUDEOILM15OCT2611000CE",
    "segment": "COMMODITY",
    "exchange": "MCX",
    "credit_quantity": 0,
    "debit_quantity": 10,
    "debit_price": 45.0,
}

# What Groww actually returns when a commodity contract is asked for under the
# equity-derivatives segment. It refuses the whole batch, not the one symbol.
WRONG_SEGMENT = "Wrong segment for trading symbol: CRUDEOILM15OCT2611000CE"


class FakeGrowwMixedBook(FakeGrowwWithPositions):
    def get_positions_for_user(self, segment=None, timeout=None):
        self._guard("positions")
        return {"positions": [SHORT_CALL, CRUDE_PUT]}

    def get_ltp(self, exchange_trading_symbols=(), segment=None, timeout=None):
        self.ltp_segments.append(segment)
        wrong = [
            k for k in exchange_trading_symbols if k.startswith("MCX_") != (segment == "COMMODITY")
        ]
        if wrong:
            raise RuntimeError(WRONG_SEGMENT)
        if segment == "COMMODITY":
            return {"MCX_CRUDEOILM15OCT2611000CE": 30.0}
        return {"NSE_NIFTY25SEP25000CE": 80.0}


@pytest.mark.asyncio
async def test_each_segment_is_priced_on_its_own_call(real_index):
    """An MCX contract and an NSE option in one book must both get a price."""
    client = FakeGrowwMixedBook()
    snap = await read_book(tools_for(client), real_index)

    # read_book also prices holdings under CASH; what matters is that the two
    # position segments each got their own call.
    assert {"COMMODITY", "FNO"} <= set(client.ltp_segments)
    priced = {r.symbol: r.ltp for r in snap.positions}
    assert priced["NIFTY25SEP25000CE"] == 80.0
    assert priced["CRUDEOILM15OCT2611000CE"] == 30.0


@pytest.mark.asyncio
async def test_one_dead_segment_does_not_cost_the_other(real_index):
    """Losing commodity prices must not take the equity positions with it."""

    class OnlyCommodityFails(FakeGrowwMixedBook):
        def get_ltp(self, exchange_trading_symbols=(), segment=None, timeout=None):
            if segment == "COMMODITY":
                raise RuntimeError("service unavailable")
            return super().get_ltp(exchange_trading_symbols, segment, timeout)

    snap = await read_book(tools_for(OnlyCommodityFails()), real_index)
    assert [r.symbol for r in snap.positions] == ["NIFTY25SEP25000CE"]
