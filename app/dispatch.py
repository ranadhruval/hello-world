"""Fast-path dispatch: route -> tools -> typed object -> template.

No model is involved in any of this (spec §6.1). The LLM path receives the
same typed objects and may add one line of prose; it never produces a figure.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.book import SEGMENT_CASH, read_portfolio, read_positions, spot
from app.channel.base import OutboundMessage
from app.market import calendar
from app.render import templates as tpl
from app.router.fastpath import Intent, Route
from app.tools.groww import GrowwTools, ToolError
from app.tools.instruments import InstrumentIndex
from app.tools.pnl import margin_utilisation
from app.tools.types import Holding, Position

log = logging.getLogger(__name__)

INDEX_BASKET = ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]


@dataclass
class Book:
    """What the user holds, cached per turn.

    Resolution uses it to rank candidates, so "nifty" from someone with a
    NIFTY position surfaces the position rather than the index.
    """

    holdings: list[Holding]
    positions: list[Position]

    @property
    def symbols(self) -> set[str]:
        return {h.trading_symbol.upper() for h in self.holdings} | {
            p.trading_symbol.upper() for p in self.positions
        }

    @property
    def venues(self) -> set[tuple[str, str]]:
        return {
            (p.exchange.upper(), p.trading_symbol.upper()) for p in self.positions if p.exchange
        }


class Desk:
    def __init__(self, tools: GrowwTools, index: InstrumentIndex) -> None:
        self._tools = tools
        self._index = index

    async def handle(self, route: Route, wa_id: str) -> OutboundMessage:
        try:
            return await self._handle(route, wa_id)
        except ToolError as exc:
            # One "couldn't fetch that" line, then the recovery action — never
            # a plausible-looking answer built on stale data (spec §1.3 rule 5).
            log.warning("tool failure intent=%s: %s", route.intent, exc)
            return _text(wa_id, _error_line(exc))

    async def _handle(self, route: Route, wa_id: str) -> OutboundMessage:
        match route.intent:
            case Intent.PORTFOLIO_SUMMARY:
                return _text(
                    wa_id, tpl.portfolio_summary(await read_portfolio(self._tools, self._index))
                )
            case Intent.PORTFOLIO_DAY_CHANGE:
                return _text(
                    wa_id, tpl.portfolio_day_change(await read_portfolio(self._tools, self._index))
                )
            case Intent.POSITIONS_OPEN:
                return _text(wa_id, await self._positions())
            case Intent.MARGIN_AVAILABLE:
                margin = await self._tools.get_margin()
                return _text(wa_id, tpl.margin_available(margin_utilisation(margin)))
            case Intent.MARGIN_UTILISATION:
                margin = await self._tools.get_margin()
                return _text(wa_id, tpl.margin_utilisation(margin_utilisation(margin)))
            case Intent.ORDERS_OPEN:
                orders = await self._tools.get_orders()
                return _text(wa_id, tpl.orders_open([o for o in orders if o.is_open]))
            case Intent.ORDERS_HISTORY:
                return _text(wa_id, tpl.orders_open(await self._tools.get_orders()))
            case Intent.MARKET_INDEX:
                return _text(wa_id, await self._indices())
            case Intent.MARKET_QUOTE | Intent.PORTFOLIO_HOLDING:
                return await self._quote(route.query, wa_id)
            case Intent.META_HELP:
                return _text(wa_id, tpl.HELP)
            case Intent.REJECT:
                return _text(wa_id, tpl.OUT_OF_SCOPE)
            case _:
                return _text(wa_id, tpl.OUT_OF_SCOPE)

    # ---- intent implementations ------------------------------------

    async def _book(self) -> Book:
        holdings, positions = await asyncio.gather(
            self._tools.get_holdings(), self._tools.get_positions()
        )
        return Book(holdings, positions)

    async def _positions(self) -> str:
        rows = await read_positions(self._tools)
        if not rows:
            return "No open positions."
        return tpl.positions_open(rows, ts=calendar.now_ist())

    async def _indices(self) -> str:
        quotes = []
        for symbol in INDEX_BASKET:
            inst = spot(self._index, symbol)
            if inst is None:
                continue
            try:
                quotes.append(await self._tools.get_quote(symbol, inst.exchange, inst.segment))
            except ToolError:
                continue
        if not quotes:
            raise ToolError("no index quotes available")
        return tpl.market_index(
            quotes, ts=calendar.now_ist(), closed_label=calendar.close_label(SEGMENT_CASH)
        )

    async def _quote(self, query: str, wa_id: str) -> OutboundMessage:
        book = await self._book()
        res = self._index.resolve(query, user_symbols=book.symbols, user_venues=book.venues)

        if res.ambiguous:
            # Never guess on a close call — send a list message (spec §1.5).
            text, rows = tpl.disambiguate(query, [c.instrument for c in res.candidates])
            return OutboundMessage(wa_id=wa_id, kind="list", text=text, list_rows=rows)
        if res.instrument is None:
            return _text(wa_id, tpl.not_found(query))

        inst = res.instrument
        quote = await self._tools.get_quote(inst.trading_symbol, inst.exchange, inst.segment)
        closed = calendar.close_label(inst.segment, underlying=inst.underlying or "")
        return _text(wa_id, tpl.market_quote(quote, closed_label=closed))


def _text(wa_id: str, body: str) -> OutboundMessage:
    return OutboundMessage(wa_id=wa_id, kind="text", text=body)


def _error_line(exc: ToolError) -> str:
    msg = str(exc).lower()
    if "rate limit" in msg:
        return tpl.RATE_LIMITED
    if "auth" in msg or "401" in msg:
        return tpl.RECONNECT
    return "Couldn't fetch that just now. Try again in a moment."
