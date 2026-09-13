"""What the user is actually exposed to (spec §16.1 materiality).

This is the join that makes the whole thing worth building. An external engine
can tell us TITAN fell 3.2% on four times normal volume; only this module knows
that the user holds 40 shares at ₹2,618, that it is 6% of their book, and that
a 3.2% day is unremarkable for them. The same signal is an interruption for one
person and noise for another, and the difference is computed here.

Nothing in this module is a threshold. Everything is relative to the user's own
book, because "big" is not a property of a number — ₹50,000 means nothing to one
book and everything to another.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.tools.pnl import PortfolioPnl, PositionPnl

# Spec §16.1: materiality scales severity by how large this is *for this user*,
# bounded so a huge position cannot dominate and a tiny one cannot vanish.
MATERIALITY_FLOOR = 0.6
MATERIALITY_CEIL = 1.6

# Below this share of the book, a name is not worth an interrupt on its own
# however dramatic the move. A 20% day in a ₹2,000 holding is a fun fact.
NEGLIGIBLE_SHARE = 0.005

# A name at this share of the book is maximally material regardless of how it
# compares to the user's median position.
DOMINANT_SHARE = 0.25

# Account-level events (margin, day P&L) are about the whole book, so there is
# no per-name notional to compare. They score neutral — neither boosted like a
# concentrated holding nor penalised like a name the user does not own.
ACCOUNT = "ACCOUNT"


@dataclass(frozen=True)
class Exposure:
    symbol: str
    segment: str
    qty: float
    notional: float  # rupees at risk right now
    share_of_book: float  # 0..1
    cost: float
    unrealised: float
    unrealised_pct: float
    is_open_position: bool = False  # F&O leg rather than a holding

    @property
    def is_negligible(self) -> bool:
        return self.share_of_book < NEGLIGIBLE_SHARE


@dataclass
class Book:
    """A user's exposure, as of one evaluation."""

    total_value: float = 0.0
    exposures: dict[str, Exposure] = field(default_factory=dict)
    typical_notional: float = 0.0
    daily_sigma: float | None = None  # of day P&L, in rupees. None when unknown.
    day_pnl: float = 0.0

    def holds(self, symbol: str) -> bool:
        return symbol.upper() in self.exposures

    def get(self, symbol: str) -> Exposure | None:
        return self.exposures.get(symbol.upper())

    def materiality(self, symbol: str) -> float:
        """Gate multiplier for how much this name matters to this user.

        Two questions, and the answer is the larger of them:

          is this a big bet *for them*   spec §16.1, notional vs median position
          does it move their net worth   share of the whole book

        The spec's formula alone is degenerate on a small book: with three
        positions the median is often the position being scored, so the ratio
        is 1.0 and a name that is a third of someone's wealth scores the same
        as a token holding. The share term is what stops that.

        A name the user does not hold scores at the floor rather than zero —
        market context still has some value, it just never interrupts alone.
        """
        if symbol.upper() == ACCOUNT:
            return 1.0
        exp = self.get(symbol)
        if exp is None:
            return MATERIALITY_FLOOR
        by_typical = (
            MATERIALITY_FLOOR + 0.4 * (exp.notional / max(self.typical_notional, 1.0))
            if self.typical_notional > 0
            else MATERIALITY_FLOOR
        )
        by_share = MATERIALITY_FLOOR + (MATERIALITY_CEIL - MATERIALITY_FLOOR) * (
            exp.share_of_book / DOMINANT_SHARE
        )
        return min(max(by_typical, by_share, MATERIALITY_FLOOR), MATERIALITY_CEIL)

    def sigma_of(self, rupees: float) -> float | None:
        """A rupee move expressed in the user's own daily P&L sigmas.

        None when we have no history — which the caller must treat as "cannot
        say", never as "not significant". This is the difference between a
        personalised system and a threshold system, and it is ten lines.
        """
        if not self.daily_sigma or self.daily_sigma <= 0:
            return None
        return abs(rupees) / self.daily_sigma

    @property
    def symbols(self) -> list[str]:
        return sorted(self.exposures)


def build_book(
    portfolio: PortfolioPnl,
    positions: list[PositionPnl] | None = None,
    *,
    daily_pnl_history: list[float] | None = None,
) -> Book:
    """Assemble the exposure view from Phase 1's already-computed P&L.

    Deliberately consumes `PortfolioPnl`/`PositionPnl` rather than raw holdings:
    that arithmetic is reconciled against the broker and pinned by tests, and
    recomputing it here would create a second source of truth for numbers that
    must agree to the rupee.
    """
    positions = positions or []
    exposures: dict[str, Exposure] = {}

    for h in portfolio.holdings:
        exposures[h.symbol.upper()] = Exposure(
            symbol=h.symbol,
            segment="CASH",
            qty=h.qty,
            notional=h.current_value,
            share_of_book=0.0,  # filled below, once the total is known
            cost=h.cost,
            unrealised=h.unrealised,
            unrealised_pct=h.unrealised_pct,
        )

    for p in positions:
        if not p.net_qty:
            continue
        # A short leg is exposure too, so notional is absolute.
        notional = abs(p.net_qty * p.ltp)
        exposures[p.symbol.upper()] = Exposure(
            symbol=p.symbol,
            segment=p.segment,
            qty=p.net_qty,
            notional=notional,
            share_of_book=0.0,
            cost=abs(p.net_qty * p.basis),
            unrealised=p.unrealised,
            unrealised_pct=(p.unrealised / abs(p.net_qty * p.basis) * 100)
            if p.basis and p.net_qty
            else 0.0,
            is_open_position=True,
        )

    total = sum(e.notional for e in exposures.values())
    if total > 0:
        exposures = {
            k: Exposure(**{**e.__dict__, "share_of_book": e.notional / total})
            for k, e in exposures.items()
        }

    notionals = sorted(e.notional for e in exposures.values() if e.notional > 0)

    return Book(
        total_value=total,
        exposures=exposures,
        typical_notional=statistics.median(notionals) if notionals else 0.0,
        daily_sigma=_sigma(daily_pnl_history),
        day_pnl=portfolio.day_change,
    )


def _sigma(history: list[float] | None) -> float | None:
    """Standard deviation of day P&L, or None.

    Needs a real sample: two days of history produce a number that looks like a
    threshold and behaves like a coin flip. Below the floor we return None and
    every caller degrades to "cannot say".
    """
    if not history or len(history) < 10:
        return None
    try:
        sd = statistics.pstdev(history)
    except statistics.StatisticsError:
        return None
    return sd if sd > 0 else None
