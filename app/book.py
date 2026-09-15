"""Read the user's book once, priced, for both the desk and the briefs.

Holdings carry no exchange, so their LTP key is rebuilt from the instrument
master (NSE first, then BSE). Positions carry an exchange and a segment and are
priced off the F&O LTP endpoint. Both callers used to do this separately and
drifted: the brief priced options off CASH quotes and never fetched day changes.
One reader, one set of rules (spec §5.3 gotcha 1).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.market import calendar
from app.tools.groww import GrowwTools, ToolError
from app.tools.instruments import InstrumentIndex
from app.tools.pnl import (
    MarginUtilisation,
    PortfolioPnl,
    PositionPnl,
    margin_utilisation,
    portfolio_pnl,
    position_pnl,
)
from app.tools.types import Instrument

log = logging.getLogger(__name__)

SEGMENT_CASH = "CASH"
SEGMENT_FNO = "FNO"
SEGMENT_COMMODITY = "COMMODITY"

DAY_CHANGE_QUOTES = 8  # get_ltp carries no day change; quote only the largest lines


@dataclass(frozen=True)
class BookSnapshot:
    portfolio: PortfolioPnl
    positions: list[PositionPnl]
    margin: MarginUtilisation | None = None


async def read_book(
    tools: GrowwTools, index: InstrumentIndex, *, day_changes: bool = True, margin: bool = False
) -> BookSnapshot:
    """Holdings and open positions priced at LTP; margin only when asked.

    Margin is fetched separately and allowed to fail: a missing margin line
    costs one section of a brief, not the brief.
    """
    portfolio, positions = await asyncio.gather(
        read_portfolio(tools, index, day_changes=day_changes), read_positions(tools)
    )
    util = None
    if margin:
        try:
            util = margin_utilisation(await tools.get_margin())
        except ToolError as exc:
            log.warning("margin unavailable: %s", exc)
    return BookSnapshot(portfolio, positions, util)


async def read_portfolio(
    tools: GrowwTools, index: InstrumentIndex, *, day_changes: bool = True
) -> PortfolioPnl:
    holdings = await tools.get_holdings()
    if not holdings:
        return portfolio_pnl([], {}, {}, as_of=calendar.now_ist())

    keyed = {h.trading_symbol: equity_key(index, h.trading_symbol) for h in holdings}
    resolved = {s: k for s, k in keyed.items() if k}
    quotes = await tools.get_ltp(list(resolved.values()), SEGMENT_CASH)
    ltps = {symbol: quotes[key] for symbol, key in resolved.items() if key in quotes}

    changes = (
        await _day_changes(tools, index, list(ltps)[:DAY_CHANGE_QUOTES]) if day_changes else {}
    )
    return portfolio_pnl(holdings, ltps, changes, as_of=calendar.now_ist())


async def read_positions(tools: GrowwTools) -> list[PositionPnl]:
    """Open positions with P&L, largest absolute P&L first. Unpriced legs are dropped."""
    positions = await tools.get_positions()
    live = [p for p in positions if (p.credit_quantity - p.debit_quantity) != 0]
    if not live:
        return []
    keys = [f"{p.exchange or 'NSE'}_{p.trading_symbol}" for p in live]
    ltps = await tools.get_ltp(keys, live[0].segment or SEGMENT_FNO)
    rows = [position_pnl(p, ltps[k]) for p, k in zip(live, keys, strict=True) if k in ltps]
    rows.sort(key=lambda r: -abs(r.total))
    return rows


async def _day_changes(
    tools: GrowwTools, index: InstrumentIndex, symbols: list[str]
) -> dict[str, float]:
    async def one(symbol: str) -> tuple[str, float]:
        inst = spot(index, symbol)
        if inst is None:
            return symbol, 0.0
        try:
            return symbol, (await tools.get_quote(symbol, inst.exchange, inst.segment)).day_change
        except ToolError:
            return symbol, 0.0

    return dict(await asyncio.gather(*(one(s) for s in symbols)))


def spot(index: InstrumentIndex, symbol: str) -> Instrument | None:
    """The cash-market instrument for a symbol: NSE before BSE, equity before commodity."""
    for exchange in ("NSE", "BSE"):
        for segment in (SEGMENT_CASH, SEGMENT_COMMODITY):
            inst = index.by_key.get((exchange, segment, symbol))
            if inst is not None:
                return inst
    return None


def equity_key(index: InstrumentIndex, symbol: str) -> str | None:
    inst = spot(index, symbol)
    if inst is None:
        log.warning("no instrument master entry for holding %s", symbol)
        return None
    return inst.ltp_key
