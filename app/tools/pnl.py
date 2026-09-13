"""P&L computation (spec §5.3).

Groww's holdings payload carries no LTP and no current value. Its positions
payload carries realised P&L but no unrealised P&L and no LTP. So every P&L
number this bot shows is computed here, by joining position/holding state to a
live quote. If these formulas are wrong, the bot lies confidently — which is
the one failure class that kills a finance assistant on day one.

Nothing in this module talks to the network. Callers pass quotes in, so every
number is reproducible from a frozen fixture and testable to the rupee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.tools.types import Holding, MarginState, Position


class Basis(StrEnum):
    """How to read Groww's credit_price / debit_price.

    UNVERIFIED against a live account — this is Appendix B item 4 and the
    §5.3 gotcha-5 reconciliation. AVERAGE treats the field as a per-unit
    average price; NOTIONAL treats it as the total value of that leg.
    scripts/reconcile.py computes both against a real book so you can see
    which one matches the app, then pin it here.
    """

    AVERAGE = "average"
    NOTIONAL = "notional"


DEFAULT_BASIS = Basis.AVERAGE


@dataclass(frozen=True)
class HoldingPnl:
    symbol: str
    qty: float
    avg_price: float
    ltp: float
    current_value: float
    cost: float
    unrealised: float
    unrealised_pct: float
    day_change: float
    day_change_pct: float
    pledged: float
    t1: float

    @property
    def is_pledged(self) -> bool:
        return self.pledged > 0


@dataclass(frozen=True)
class PositionPnl:
    symbol: str
    segment: str
    net_qty: float
    basis: float
    ltp: float
    unrealised: float
    realised: float
    total: float

    @property
    def direction(self) -> str:
        if self.net_qty > 0:
            return "long"
        if self.net_qty < 0:
            return "short"
        return "flat"


@dataclass
class PortfolioPnl:
    current_value: float
    cost: float
    unrealised: float
    unrealised_pct: float
    day_change: float
    day_change_pct: float
    holdings: list[HoldingPnl] = field(default_factory=list)
    as_of: datetime | None = None
    missing_quotes: list[str] = field(default_factory=list)

    @property
    def movers(self) -> list[HoldingPnl]:
        return sorted(self.holdings, key=lambda h: abs(h.day_change), reverse=True)


def holding_pnl(h: Holding, ltp: float, day_change: float) -> HoldingPnl:
    """Value a holding against a live price.

    `day_change` is the instrument's rupee move for the session (quote.day_change),
    per unit — it needs the quantity multiply that Groww does not do for you.
    Pledged quantity still counts toward value; it just is not freely sellable.
    """
    qty = h.quantity
    value = qty * ltp
    cost = qty * h.average_price
    prev_close = ltp - day_change
    prev_value = qty * prev_close
    return HoldingPnl(
        symbol=h.trading_symbol,
        qty=qty,
        avg_price=h.average_price,
        ltp=ltp,
        current_value=value,
        cost=cost,
        unrealised=value - cost,
        unrealised_pct=(value - cost) / cost * 100 if cost else 0.0,
        day_change=qty * day_change,
        day_change_pct=(value - prev_value) / prev_value * 100 if prev_value else 0.0,
        pledged=h.pledge_quantity,
        t1=h.t1_quantity,
    )


def open_basis(p: Position, convention: Basis = DEFAULT_BASIS) -> tuple[float, float]:
    """Return (net_qty, per-unit cost basis of the leg that remains open).

    Groww models positions as credit (bought) vs debit (sold) legs.
    net_qty > 0 is net long, < 0 is net short. The basis is the average of
    whichever side is still open.
    """
    net_qty = p.credit_quantity - p.debit_quantity
    if net_qty > 0:
        raw, qty = p.credit_price, p.credit_quantity
    elif net_qty < 0:
        raw, qty = p.debit_price, p.debit_quantity
    else:
        return 0.0, 0.0

    if convention is Basis.NOTIONAL and qty:
        return net_qty, raw / qty
    return net_qty, raw


def position_pnl(p: Position, ltp: float, convention: Basis = DEFAULT_BASIS) -> PositionPnl:
    net_qty, basis = open_basis(p, convention)
    if net_qty > 0:
        unrealised = net_qty * (ltp - basis)
    elif net_qty < 0:
        unrealised = abs(net_qty) * (basis - ltp)
    else:
        unrealised = 0.0
    return PositionPnl(
        symbol=p.trading_symbol,
        segment=p.segment,
        net_qty=net_qty,
        basis=basis,
        ltp=ltp,
        unrealised=unrealised,
        realised=p.realised_pnl,  # trust theirs
        total=unrealised + p.realised_pnl,
    )


def portfolio_pnl(
    holdings: list[Holding],
    ltps: dict[str, float],
    day_changes: dict[str, float],
    as_of: datetime | None = None,
) -> PortfolioPnl:
    """Aggregate a book. Keys of `ltps`/`day_changes` are trading symbols.

    A holding with no quote is excluded from the totals and named in
    `missing_quotes` — never valued at zero, never silently dropped (I5).
    """
    rows: list[HoldingPnl] = []
    missing: list[str] = []
    for h in holdings:
        ltp = ltps.get(h.trading_symbol)
        if ltp is None:
            missing.append(h.trading_symbol)
            continue
        rows.append(holding_pnl(h, ltp, day_changes.get(h.trading_symbol, 0.0)))

    value = sum(r.current_value for r in rows)
    cost = sum(r.cost for r in rows)
    day = sum(r.day_change for r in rows)
    prev_value = value - day
    return PortfolioPnl(
        current_value=value,
        cost=cost,
        unrealised=value - cost,
        unrealised_pct=(value - cost) / cost * 100 if cost else 0.0,
        day_change=day,
        day_change_pct=day / prev_value * 100 if prev_value else 0.0,
        holdings=rows,
        as_of=as_of,
        missing_quotes=missing,
    )


@dataclass(frozen=True)
class MarginUtilisation:
    utilisation: float  # 0..1
    used: float
    headroom: float
    total_capital: float
    span: float
    exposure: float
    fno_used: float
    sell_headroom: float
    collateral_available: float


def margin_utilisation(m: MarginState) -> MarginUtilisation:
    """Derive utilisation from the margin payload (spec §5.3).

    Calibrate against what the Groww app shows before building an alert on
    this — the Phase 2 margin bands are meaningless until it matches.
    """
    used = m.net_margin_used
    headroom = m.clear_cash + m.collateral_available
    denom = used + headroom
    return MarginUtilisation(
        utilisation=used / denom if denom else 0.0,
        used=used,
        headroom=headroom,
        total_capital=m.clear_cash + m.collateral_available + m.adhoc_margin,
        span=m.fno.span_margin_used,
        exposure=m.fno.exposure_margin_used,
        fno_used=m.fno.net_fno_margin_used,
        sell_headroom=m.fno.option_sell_balance_available,
        collateral_available=m.collateral_available,
    )
