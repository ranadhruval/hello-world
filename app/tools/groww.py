"""Typed wrappers over growwapi (spec §5.1, §5.2).

Every method returns a dataclass from app.tools.types, never a string and
never raw JSON. Each call is cached, rate-limited against its type-group and
guarded by a circuit breaker. SDK calls are blocking, so they run in a thread.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import datetime
from typing import Any, TypeVar

from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIRateLimitException,
)

from app.config import BATCH_LIMIT, IST, settings
from app.infra import Cache, CircuitBreaker, CircuitOpen, RateLimiter, RateLimitExceeded
from app.tools.types import (
    OHLC,
    Greeks,
    Holding,
    MarginState,
    OptionChain,
    Order,
    Position,
    Quote,
)

log = logging.getLogger(__name__)
T = TypeVar("T")

LIVE = "live_data"
NON_TRADING = "non_trading"


class ToolError(RuntimeError):
    """A tool failed and there is no cached value to serve.

    Surfaced to the user as a visible error — never as a plausible-looking
    message built on stale data (invariant I5).
    """


class GrowwTools:
    def __init__(
        self,
        broker,
        user_id: int,
        cache: Cache | None = None,
        limiter: RateLimiter | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._broker = broker
        self._user_id = user_id
        self._cache = cache or Cache()
        self._limiter = limiter or RateLimiter()
        self._breaker = breaker or CircuitBreaker()

    # ---- portfolio -------------------------------------------------

    async def get_holdings(self) -> list[Holding]:
        raw = await self._call(
            NON_TRADING,
            settings().ttl_holdings,
            "holdings",
            lambda c: c.get_holdings_for_user(timeout=5),
        )
        return [Holding.from_api(d) for d in _rows(raw, "holdings")]

    async def get_positions(self, segment: str | None = None) -> list[Position]:
        raw = await self._call(
            NON_TRADING,
            settings().ttl_positions,
            f"positions:{segment or 'all'}",
            lambda c: c.get_positions_for_user(segment=segment, timeout=5),
        )
        return [Position.from_api(d) for d in _rows(raw, "positions")]

    async def get_position(self, trading_symbol: str, segment: str) -> Position | None:
        raw = await self._call(
            NON_TRADING,
            settings().ttl_positions,
            f"position:{segment}:{trading_symbol}",
            lambda c: c.get_position_for_trading_symbol(
                trading_symbol=trading_symbol, segment=segment, timeout=5
            ),
        )
        rows = _rows(raw, "positions")
        return Position.from_api(rows[0]) if rows else None

    async def get_margin(self) -> MarginState:
        raw = await self._call(
            NON_TRADING,
            settings().ttl_margin,
            "margin",
            lambda c: c.get_available_margin_details(timeout=5),
        )
        return MarginState.from_api(raw if isinstance(raw, dict) else {})

    async def get_orders(self, segment: str | None = None, page_size: int = 50) -> list[Order]:
        raw = await self._call(
            NON_TRADING,
            settings().ttl_orders,
            f"orders:{segment or 'all'}:{page_size}",
            lambda c: c.get_order_list(page=0, page_size=page_size, segment=segment, timeout=5),
        )
        return [Order.from_api(d) for d in _rows(raw, "order_list", "orders")]

    # ---- live data -------------------------------------------------

    async def get_ltp(self, keys: list[str], segment: str) -> dict[str, float]:
        """Batched LTP. `keys` are 'EXCHANGE_SYMBOL', e.g. 'NSE_RELIANCE'.

        get_ltp takes at most 50 instruments and one segment per call, so a
        mixed-segment portfolio needs one call per segment — still never one
        call per holding.
        """
        out: dict[str, float] = {}
        for chunk in _chunks(sorted(set(keys)), BATCH_LIMIT):
            raw = await self._call(
                LIVE,
                settings().ttl_ltp,
                f"ltp:{segment}:{'|'.join(chunk)}",
                lambda c, ch=tuple(chunk): c.get_ltp(
                    exchange_trading_symbols=ch, segment=segment, timeout=5
                ),
            )
            if isinstance(raw, dict):
                out.update({k: float(v) for k, v in raw.items() if v is not None})
        return out

    async def get_ohlc(self, keys: list[str], segment: str) -> dict[str, OHLC]:
        out: dict[str, OHLC] = {}
        for chunk in _chunks(sorted(set(keys)), BATCH_LIMIT):
            raw = await self._call(
                LIVE,
                settings().ttl_ohlc,
                f"ohlc:{segment}:{'|'.join(chunk)}",
                lambda c, ch=tuple(chunk): c.get_ohlc(
                    exchange_trading_symbols=ch, segment=segment, timeout=5
                ),
            )
            if isinstance(raw, dict):
                out.update({k: OHLC.from_api(v) for k, v in raw.items() if isinstance(v, dict)})
        return out

    async def get_quote(self, trading_symbol: str, exchange: str, segment: str) -> Quote:
        raw = await self._call(
            LIVE,
            settings().ttl_quote,
            f"quote:{exchange}:{segment}:{trading_symbol}",
            lambda c: c.get_quote(
                trading_symbol=trading_symbol, exchange=exchange, segment=segment, timeout=5
            ),
        )
        quote = Quote.from_api(raw if isinstance(raw, dict) else {}, trading_symbol)
        return _stamp(quote)

    async def get_option_chain(self, exchange: str, underlying: str, expiry: str) -> OptionChain:
        raw = await self._call(
            LIVE,
            settings().ttl_option_chain,
            f"chain:{exchange}:{underlying}:{expiry}",
            lambda c: c.get_option_chain(
                exchange=exchange, underlying=underlying, expiry_date=expiry, timeout=5
            ),
        )
        chain = OptionChain.from_api(raw if isinstance(raw, dict) else {}, underlying, expiry)
        return _stamp(chain)

    async def get_greeks(
        self, exchange: str, underlying: str, trading_symbol: str, expiry: str
    ) -> Greeks:
        raw = await self._call(
            LIVE,
            settings().ttl_greeks,
            f"greeks:{exchange}:{trading_symbol}:{expiry}",
            lambda c: c.get_greeks(
                exchange=exchange,
                underlying=underlying,
                trading_symbol=trading_symbol,
                expiry=expiry,
            ),
        )
        return Greeks.from_api(raw if isinstance(raw, dict) else {})

    async def get_expiries(self, exchange: str, underlying: str) -> list[str]:
        raw = await self._call(
            LIVE,
            3600,
            f"expiries:{exchange}:{underlying}",
            lambda c: c.get_expiries(exchange=exchange, underlying_symbol=underlying, timeout=5),
        )
        rows = _rows(raw, "expiries", "expiry_dates")
        return [r if isinstance(r, str) else str(r) for r in rows]

    # ---- plumbing --------------------------------------------------

    async def _call(self, group: str, ttl: int, cache_key: str, fn) -> Any:
        key = f"u{self._user_id}:{cache_key}"

        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        try:
            self._breaker.guard()
            await self._limiter.check(group)
        except (CircuitOpen, RateLimitExceeded) as exc:
            # Budget dry or upstream sick: there is nothing cached to fall back
            # on, so fail loudly rather than inventing a number (I5).
            raise ToolError(str(exc)) from exc

        try:
            result = await self._invoke(fn)
        except GrowwAPIAuthenticationException:
            # Access token expired mid-request: re-mint once, retry once, then fail.
            await self._broker.invalidate(self._user_id)
            result = await self._invoke(fn)
        except GrowwAPIRateLimitException as exc:
            self._breaker.record_failure()
            raise ToolError("Groww is rate-limiting") from exc
        except Exception as exc:
            self._breaker.record_failure()
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

        self._breaker.record_success()
        await self._cache.set(key, result, ttl)
        return result

    async def _invoke(self, fn) -> Any:
        client = await self._broker.client(self._user_id)
        return await asyncio.to_thread(fn, client)


def _rows(raw: Any, *keys: str) -> list[dict]:
    """Groww wraps collections under a payload key that varies by endpoint."""
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        for v in raw.values():
            if isinstance(v, list) and all(isinstance(r, dict) for r in v):
                return v
    return []


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _stamp(obj: T) -> T:
    return dataclasses.replace(obj, as_of=datetime.now(IST))
