"""End-to-end: inbound text -> router -> tools -> P&L -> rendered message.

A fake Groww client stands in for the network, so the whole pipeline runs
deterministically and every rendered figure is checkable against arithmetic.
"""

import pytest

from app.channel.console import ConsoleChannel, inbound
from app.dispatch import Desk
from app.infra import Cache, CircuitBreaker, MemoryBackend, RateLimiter
from app.render.templates import inr
from app.router.fastpath import Intent, classify
from app.tools.groww import GrowwTools, ToolError
from app.worker import Worker

HOLDINGS = [
    {"trading_symbol": "RELIANCE", "quantity": 10, "average_price": 1000.0},
    {"trading_symbol": "INFY", "quantity": 20, "average_price": 1500.0, "pledge_quantity": 5},
]
QUOTES = {
    "RELIANCE": {"last_price": 1100.0, "day_change": 25.0, "day_change_perc": 2.33,
                 "ohlc": {"open": 1080.0, "high": 1105.0, "low": 1075.0, "close": 1075.0}},
    "INFY": {"last_price": 1450.0, "day_change": -10.0, "day_change_perc": -0.68,
             "ohlc": {"open": 1462.0, "high": 1470.0, "low": 1445.0, "close": 1460.0}},
    "NIFTY": {"last_price": 25100.0, "day_change": 120.0, "day_change_perc": 0.48,
              "ohlc": {"open": 25000.0, "high": 25150.0, "low": 24980.0, "close": 24980.0}},
}


class FakeGroww:
    """Mirrors the growwapi surface the tool layer actually calls."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.calls: list[str] = []

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_with:
            raise self.fail_with

    def get_holdings_for_user(self, timeout=None):
        self._guard("holdings")
        return {"holdings": HOLDINGS}

    def get_positions_for_user(self, segment=None, timeout=None):
        self._guard("positions")
        return {"positions": []}

    def get_available_margin_details(self, timeout=None):
        self._guard("margin")
        return {
            "clear_cash": 100_000.0,
            "collateral_available": 300_000.0,
            "net_margin_used": 600_000.0,
            "fno_margin_details": {"span_margin_used": 412_000.0,
                                   "exposure_margin_used": 88_000.0},
        }

    def get_order_list(self, page=0, page_size=25, segment=None, timeout=None):
        self._guard("orders")
        return {"order_list": []}

    def get_ltp(self, exchange_trading_symbols=(), segment=None, timeout=None):
        self._guard("ltp")
        out = {}
        for key in exchange_trading_symbols:
            symbol = key.split("_", 1)[1]
            if symbol in QUOTES:
                out[key] = QUOTES[symbol]["last_price"]
        return out

    def get_quote(self, trading_symbol=None, exchange=None, segment=None, timeout=None):
        self._guard("quote")
        if trading_symbol not in QUOTES:
            raise KeyError(trading_symbol)
        return {**QUOTES[trading_symbol], "trading_symbol": trading_symbol}


class FakeBroker:
    def __init__(self, client) -> None:
        self.client_obj = client
        self.invalidated = 0

    async def client(self, user_id: int):
        return self.client_obj

    async def invalidate(self, user_id: int) -> None:
        self.invalidated += 1


def make_desk(real_index, client=None) -> Desk:
    client = client or FakeGroww()
    tools = GrowwTools(
        FakeBroker(client),
        user_id=1,
        cache=Cache(MemoryBackend()),
        limiter=RateLimiter(MemoryBackend()),
        breaker=CircuitBreaker(),
    )
    return Desk(tools, real_index)


async def ask(desk: Desk, text: str) -> str:
    return (await desk.handle(classify(text), "919999999999")).text


# ---- portfolio -----------------------------------------------------


async def test_portfolio_renders_the_computed_total(real_index):
    out = await ask(make_desk(real_index), "portfolio")
    # 10 x 1100 + 20 x 1450 = 40,000
    assert inr(40_000) in out
    assert "2 holdings" in out


async def test_portfolio_day_change_is_quantity_weighted(real_index):
    out = await ask(make_desk(real_index), "pnl")
    # 10 x 25 + 20 x -10 = +50
    assert "+₹50" in out


async def test_empty_portfolio_does_not_fabricate(real_index):
    client = FakeGroww()
    client.get_holdings_for_user = lambda timeout=None: {"holdings": []}
    out = await ask(make_desk(real_index, client), "portfolio")
    assert inr(0) in out


# ---- margin --------------------------------------------------------


async def test_margin_renders_utilisation(real_index):
    out = await ask(make_desk(real_index), "margin")
    assert "60%" in out
    assert inr(400_000) in out  # headroom


# ---- positions and orders ------------------------------------------


async def test_no_positions_says_so_rather_than_inventing(real_index):
    assert await ask(make_desk(real_index), "positions") == "No open positions."


async def test_no_orders_says_so(real_index):
    assert await ask(make_desk(real_index), "orders") == "No open orders."


# ---- quotes and disambiguation -------------------------------------


async def test_bare_symbol_returns_a_quote(real_index):
    desk = make_desk(real_index)
    route = classify("reliance", resolves_to_instrument=lambda q: True)
    out = (await desk.handle(route, "919999999999")).text
    assert "RELIANCE" in out
    assert inr(1100.0, 2) in out


async def test_ambiguous_query_returns_a_list_not_a_guess(real_index):
    """GOLD trades on MCX and NSE — never pick one silently (spec §5.4 step 6)."""
    desk = make_desk(real_index)
    route = classify("gold", resolves_to_instrument=lambda q: True)
    out = await desk.handle(route, "919999999999")
    assert out.kind == "list"
    assert len(out.list_rows) >= 2


async def test_unknown_symbol_says_not_found(real_index):
    desk = make_desk(real_index)
    route = classify("zzzznotathing", resolves_to_instrument=lambda q: True)
    out = (await desk.handle(route, "919999999999")).text
    assert "Couldn't find" in out


# ---- failure handling ----------------------------------------------


async def test_upstream_failure_surfaces_an_error_not_a_number(real_index):
    """Invariant I5: staleness is worse than silence."""
    desk = make_desk(real_index, FakeGroww(fail_with=RuntimeError("boom")))
    out = await ask(desk, "portfolio")
    assert "₹" not in out
    assert "Couldn't fetch" in out


async def test_out_of_scope_gets_one_redirect_line(real_index):
    out = await ask(make_desk(real_index), "what's the weather")
    assert out.startswith("I only do markets")


async def test_help_lists_capabilities(real_index):
    out = await ask(make_desk(real_index), "help")
    assert "portfolio" in out and "positions" in out


# ---- caching -------------------------------------------------------


async def test_repeated_calls_hit_the_cache(real_index):
    """Live Data is capped at 300/min, so a second identical ask must not
    re-hit the network (spec §5.5)."""
    client = FakeGroww()
    desk = make_desk(real_index, client)
    await ask(desk, "margin")
    first = client.calls.count("margin")
    await ask(desk, "margin")
    assert client.calls.count("margin") == first


# ---- through the worker --------------------------------------------


async def test_worker_end_to_end(real_index):
    channel = ConsoleChannel()
    desk = make_desk(real_index)

    async def desk_for(wa_id):
        return desk

    worker = Worker(channel, real_index, desk_for=desk_for)
    await worker.handle(inbound("portfolio"))

    assert len(channel.sent) == 1
    assert inr(40_000) in channel.sent[0].text


async def test_worker_drops_duplicates(real_index):
    channel = ConsoleChannel()
    desk = make_desk(real_index)

    async def desk_for(wa_id):
        return desk

    worker = Worker(channel, real_index, desk_for=desk_for)
    msg = inbound("portfolio", msg_id="same-id")
    await worker.handle(msg)
    await worker.handle(msg)

    assert len(channel.sent) == 1


async def test_unlinked_user_gets_onboarding(real_index):
    channel = ConsoleChannel()

    async def desk_for(wa_id):
        return None

    worker = Worker(channel, real_index, desk_for=desk_for)
    await worker.handle(inbound("portfolio"))

    assert "connect your account" in channel.sent[0].text.lower()


# ---- routing sanity ------------------------------------------------


def test_classify_is_used_not_bypassed():
    assert classify("portfolio").intent is Intent.PORTFOLIO_SUMMARY


async def test_tool_error_is_raised_not_swallowed(real_index):
    tools = GrowwTools(
        FakeBroker(FakeGroww(fail_with=RuntimeError("boom"))),
        user_id=1,
        cache=Cache(MemoryBackend()),
        limiter=RateLimiter(MemoryBackend()),
        breaker=CircuitBreaker(),
    )
    with pytest.raises(ToolError):
        await tools.get_holdings()
