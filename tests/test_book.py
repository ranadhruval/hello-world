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
