"""Rules: deterministic detection (spec §13.2, §13.3).

Invariant I2 lives here. Every rule is plain Python over typed state — no model
is consulted to *notice* anything. A 60-second LLM tick is too slow for a
breached strike, too expensive across a session, and non-deterministic on
exactly the thing that must be deterministic.

Each rule is a declarative object, so adding one is a small diff that never
touches the engine. A rule returns Triggers; whether a Trigger becomes a message
is the gate's decision, not the rule's — rules must not try to be tactful, only
correct.

Two provenances, deliberately kept in one shape:
  market/news/corp  driven by an external signal (docs/SIGNAL_CONTRACT.md)
  book families     driven by the user's own margin, positions and P&L, which
                    no external source can see
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from app.tools.pnl import MarginUtilisation, PositionPnl
from app.tools.types import Greeks
from app.watcher.exposure import Book
from app.watcher.signals import Signal, SignalKind


class Family(StrEnum):
    MARGIN = "margin"
    EXPIRY = "expiry"
    STRUCTURE = "structure"
    PNL = "pnl"
    MARKET = "market"
    NEWS = "news"
    CORP = "corp"


# Spec §12.3: compare state *classes*, not raw values. A utilisation
# oscillating 69.8 -> 70.1 -> 69.9 must produce one event, not three.
MARGIN_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.00, 0.50, "comfortable"),
    (0.50, 0.70, "engaged"),
    (0.70, 0.85, "tight"),
    (0.85, 0.95, "stressed"),
    (0.95, 2.00, "critical"),
)
BAND_ORDER = [b[2] for b in MARGIN_BANDS]
BAND_SEVERITY = {"engaged": 0.3, "tight": 0.6, "stressed": 0.85, "critical": 1.0}


def margin_band(util: float) -> str:
    for lo, hi, name in MARGIN_BANDS:
        if lo <= util < hi:
            return name
    return BAND_ORDER[-1]


@dataclass(frozen=True)
class Trigger:
    rule_id: str
    entity: str
    payload: dict  # typed slots for the renderer, never prose
    magnitude: float  # 0..1, rule-local
    dedupe: str  # stable per condition, not per evaluation
    family: Family = Family.MARKET
    signal_id: str = ""


@dataclass
class Context:
    """Everything a rule may read. Rules never perform I/O."""

    book: Book
    now: datetime
    signal: Signal | None = None
    margin: MarginUtilisation | None = None
    positions: list[PositionPnl] = field(default_factory=list)
    greeks: dict[str, Greeks] = field(default_factory=dict)
    entry_greeks: dict[str, Greeks] = field(default_factory=dict)
    expiries: dict[str, date] = field(default_factory=dict)
    prev_band: str = ""
    band_held: int = 0  # consecutive evaluations in the new band
    spot: dict[str, float] = field(default_factory=dict)
    strikes: dict[str, float] = field(default_factory=dict)

    @property
    def today(self) -> date:
        return self.now.date()


@dataclass(frozen=True)
class Rule:
    id: str
    family: Family
    base_severity: float
    cooldown_s: int
    predicate: Callable[[Context], list[Trigger]]
    requires: tuple[str, ...] = ()
    market_hours_only: bool = True


REGISTRY: dict[str, Rule] = {}


def rule(
    id: str,
    family: Family,
    base_severity: float,
    cooldown_s: int,
    requires: tuple[str, ...] = (),
    market_hours_only: bool = True,
) -> Callable:
    def wrap(fn: Callable[[Context], list[Trigger]]) -> Callable:
        REGISTRY[id] = Rule(
            id=id,
            family=family,
            base_severity=base_severity,
            cooldown_s=cooldown_s,
            predicate=fn,
            requires=requires,
            market_hours_only=market_hours_only,
        )
        return fn

    return wrap


def _ratio_magnitude(x: float, mild: float, extreme: float) -> float:
    """Map a ratio onto 0..1 logarithmically.

    Linear scaling makes a 20x volume spike twenty times a 1x one, which is not
    how surprise works: the step from normal to 3x is the informative one, and
    everything past 10x is simply "a lot".
    """
    if x <= mild:
        return 0.0
    return min(1.0, math.log(x / mild) / math.log(extreme / mild))


# ----------------------------------------------------------- market family


@rule("market.volume_spike", Family.MARKET, 0.45, 3600, ("signal",))
def volume_spike(ctx: Context) -> list[Trigger]:
    """Unusual volume in a name the user holds.

    Requires a time-of-day-normalised ratio. If the source could not provide
    one we do not guess: an un-normalised ratio makes every stock look like it
    is spiking at 09:20, and an alert keyed to the clock rather than the stock
    is worse than no alert.
    """
    s = ctx.signal
    if s is None or s.kind is not SignalKind.VOLUME_SPIKE:
        return []
    rel = s.number("rel_volume")
    if rel is None or rel < 2.0:
        return []
    sym = s.entity.symbol
    exp = ctx.book.get(sym)
    if exp is None or exp.is_negligible:
        return []
    return [
        Trigger(
            rule_id="market.volume_spike",
            entity=sym,
            family=Family.MARKET,
            magnitude=_ratio_magnitude(rel, 2.0, 10.0),
            dedupe=f"volume_spike|{sym}|{ctx.today}",
            signal_id=s.source_event_id,
            payload={
                "symbol": sym,
                "rel_volume": rel,
                "price_change_pct": s.number("price_change_pct"),
                "qty": exp.qty,
                "notional": exp.notional,
                "share_of_book": exp.share_of_book,
                "unrealised_pct": exp.unrealised_pct,
            },
        )
    ]


@rule("market.breakout", Family.MARKET, 0.4, 21600, ("signal",))
def breakout(ctx: Context) -> list[Trigger]:
    s = ctx.signal
    if s is None or s.kind is not SignalKind.BREAKOUT:
        return []
    level = s.number("level_value")
    if level is None:
        return []
    sym = s.entity.symbol
    exp = ctx.book.get(sym)
    if exp is None or exp.is_negligible:
        return []
    kind = s.enum("level_type") or "level"
    return [
        Trigger(
            rule_id="market.breakout",
            entity=sym,
            family=Family.MARKET,
            magnitude=0.7 if "52w" in kind else 0.4,
            dedupe=f"breakout|{sym}|{kind}|{ctx.today}",
            signal_id=s.source_event_id,
            payload={
                "symbol": sym,
                "level_type": kind,
                "level_value": level,
                "volume_confirmed": s.payload.get("volume_confirmed"),
                "qty": exp.qty,
                "notional": exp.notional,
                "share_of_book": exp.share_of_book,
                "unrealised_pct": exp.unrealised_pct,
            },
        )
    ]


@rule("market.oi_buildup", Family.MARKET, 0.4, 3600, ("signal",))
def oi_buildup(ctx: Context) -> list[Trigger]:
    """Positioning change in an underlying the user has a position in.

    The buildup class is the signal; raw OI change on its own says nothing
    without the price direction beside it.
    """
    s = ctx.signal
    if s is None or s.kind is not SignalKind.OI:
        return []
    variant = s.enum("buildup_class")
    oi_chg = s.number("oi_change_pct")
    if variant is None or oi_chg is None:
        return []
    sym = s.entity.underlying or s.entity.symbol
    if not any(sym in k for k in ctx.book.exposures):
        return []
    return [
        Trigger(
            rule_id="market.oi_buildup",
            entity=sym,
            family=Family.MARKET,
            magnitude=_ratio_magnitude(abs(oi_chg), 5.0, 40.0),
            dedupe=f"oi|{sym}|{variant}|{ctx.today}",
            signal_id=s.source_event_id,
            payload={
                "symbol": sym,
                "buildup_class": variant,
                "oi_change_pct": oi_chg,
                "price_change_pct": s.number("price_change_pct"),
            },
        )
    ]


@rule("news.position_scoped", Family.NEWS, 0.5, 1800, ("signal",), market_hours_only=False)
def news_position_scoped(ctx: Context) -> list[Trigger]:
    """News touching something held. Everything else goes to the digest.

    Staleness matters more here than anywhere: the engine applies a seven-day
    recency window, so an item can surface days after publication, and a stale
    "breaking" alert reads as the system being asleep.
    """
    s = ctx.signal
    if s is None or s.kind not in (SignalKind.NEWS, SignalKind.ANNOUNCEMENT):
        return []
    if s.is_stale(ctx.now):
        return []
    sym = s.entity.symbol
    exp = ctx.book.get(sym)
    if exp is None or exp.is_negligible:
        return []
    category = s.enum("category") or "news"
    return [
        Trigger(
            rule_id="news.position_scoped",
            entity=sym,
            family=Family.NEWS,
            magnitude=0.7 if category in {"results", "block_deal", "regulatory", "pledge"} else 0.4,
            dedupe=f"news|{sym}|{s.source_event_id}",
            signal_id=s.source_event_id,
            payload={
                "symbol": sym,
                "headline": s.text("headline"),
                "category": category,
                "published_at": s.at,
                "qty": exp.qty,
                "notional": exp.notional,
                "share_of_book": exp.share_of_book,
                "unrealised_pct": exp.unrealised_pct,
            },
        )
    ]


@rule("corp.action", Family.CORP, 0.4, 86400, ("signal",), market_hours_only=False)
def corp_action(ctx: Context) -> list[Trigger]:
    s = ctx.signal
    if s is None or s.kind is not SignalKind.CORP_ACTION:
        return []
    sym = s.entity.symbol
    exp = ctx.book.get(sym)
    if exp is None:
        return []
    return [
        Trigger(
            rule_id="corp.action",
            entity=sym,
            family=Family.CORP,
            magnitude=0.5,
            dedupe=f"corp|{sym}|{s.text('action_type')}|{s.text('ex_date')}",
            signal_id=s.source_event_id,
            payload={
                "symbol": sym,
                "action_type": s.text("action_type"),
                "ex_date": s.text("ex_date"),
                "value": s.number("value"),
                "qty": exp.qty,
            },
        )
    ]


# ------------------------------------------------------------- book family


# Cooldown deliberately longer than the spec's 900s, because the dedupe key
# below is per band per day: at 900s an unchanged "tight" re-alerts every
# quarter hour with identical text. A *worsening* band builds a different key
# and is never blocked by this. Cooldown and dedupe granularity have to agree,
# or one of them is decoration.
@rule("margin.band_up", Family.MARGIN, 0.85, 21_600, ("margin",))
def margin_band_up(ctx: Context) -> list[Trigger]:
    """Margin utilisation entered a worse band, and stayed there.

    Hysteresis is the whole rule: a band must hold for two consecutive
    evaluations before it counts as changed. Without it, a value resting on a
    boundary produces an alert every tick.
    """
    m = ctx.margin
    if m is None or not ctx.prev_band:
        return []
    band = margin_band(m.utilisation)
    if band == ctx.prev_band:
        return []
    if BAND_ORDER.index(band) <= BAND_ORDER.index(ctx.prev_band):
        return []  # improving is not news
    if ctx.band_held < 2:
        return []
    return [
        Trigger(
            rule_id="margin.band_up",
            entity="ACCOUNT",
            family=Family.MARGIN,
            magnitude=BAND_SEVERITY.get(band, 0.5),
            dedupe=f"margin|{band}|{ctx.today}",
            payload={
                "band": band,
                "from_band": ctx.prev_band,
                "utilisation": m.utilisation,
                "used": m.used,
                "headroom": m.headroom,
                "span": m.span,
                "exposure": m.exposure,
                "collateral_available": m.collateral_available,
            },
        )
    ]


@rule("expiry.itm_short", Family.EXPIRY, 0.9, 21600, ("positions",))
def expiry_itm_short(ctx: Context) -> list[Trigger]:
    """A short option is in the money with expiry close.

    The highest-value rule in the set for an option seller, and the one whose
    absence is felt as money. Escalates T-3 through T-0 rather than firing once.
    """
    out: list[Trigger] = []
    for p in ctx.positions:
        if p.net_qty >= 0:
            continue  # long options cannot be assigned
        expiry = ctx.expiries.get(p.symbol)
        strike = ctx.strikes.get(p.symbol)
        spot = ctx.spot.get(p.symbol)
        if expiry is None or strike is None or spot is None:
            continue
        days = (expiry - ctx.today).days
        if days < 0 or days > 3:
            continue
        is_call = p.symbol.upper().endswith("CE")
        itm = spot > strike if is_call else spot < strike
        if not itm:
            continue
        sev = {3: 0.5, 2: 0.7, 1: 0.9, 0: 1.0}[days]
        out.append(
            Trigger(
                rule_id="expiry.itm_short",
                entity=p.symbol,
                family=Family.EXPIRY,
                magnitude=sev,
                # Once a day per contract; T-0 re-escalates within the day.
                dedupe=f"itm_short|{p.symbol}|{ctx.today}" + ("|t0" if days == 0 else ""),
                payload={
                    "symbol": p.symbol,
                    "days_to_expiry": days,
                    "strike": strike,
                    "spot": spot,
                    "net_qty": p.net_qty,
                    "unrealised": p.unrealised,
                    "moneyness_pct": abs(spot - strike) / strike * 100 if strike else 0.0,
                },
            )
        )
    return out


@rule("greeks.delta_drift", Family.STRUCTURE, 0.6, 3600, ("greeks",))
def delta_drift(ctx: Context) -> list[Trigger]:
    """A short option's delta has moved materially since entry.

    Risk changes shape before it shows up in P&L. A seller who opened at 0.19
    delta and is now at 0.44 has roughly twice the directional exposure they
    chose, and nothing in a P&L number says so.
    """
    out: list[Trigger] = []
    for p in ctx.positions:
        if p.net_qty >= 0:
            continue
        now_g = ctx.greeks.get(p.symbol)
        then_g = ctx.entry_greeks.get(p.symbol)
        if now_g is None or then_g is None or not then_g.delta:
            continue
        d_now, d_then = abs(now_g.delta), abs(then_g.delta)
        if d_now - d_then < 0.15:
            continue
        out.append(
            Trigger(
                rule_id="greeks.delta_drift",
                entity=p.symbol,
                family=Family.STRUCTURE,
                magnitude=min(1.0, (d_now - d_then) / 0.4),
                dedupe=f"delta|{p.symbol}|{round(d_now, 1)}",
                payload={
                    "symbol": p.symbol,
                    "delta_now": d_now,
                    "delta_entry": d_then,
                    "iv_now": now_g.iv,
                    "net_qty": p.net_qty,
                    "unrealised": p.unrealised,
                },
            )
        )
    return out


@rule("pnl.day_move", Family.PNL, 0.5, 3600, ("book",))
def pnl_day_move(ctx: Context) -> list[Trigger]:
    """Day P&L beyond the user's own 1-sigma day.

    Calibrated to the user, not to a constant. Fire at +/-1 sigma, never at
    "+/-Rs 10,000" — a fifty-thousand-rupee move means nothing to one book and
    everything to another. When we have no history we say nothing, rather than
    falling back to a number that would be a guess dressed as a threshold.
    """
    sigma = ctx.book.sigma_of(ctx.book.day_pnl)
    if sigma is None or sigma < 1.0:
        return []
    return [
        Trigger(
            rule_id="pnl.day_move",
            entity="ACCOUNT",
            family=Family.PNL,
            magnitude=min(1.0, (sigma - 1.0) / 2.0),
            dedupe=f"day_move|{ctx.today}|{int(sigma)}",
            payload={
                "day_pnl": ctx.book.day_pnl,
                "sigma": sigma,
                "total_value": ctx.book.total_value,
                "daily_sigma_rupees": ctx.book.daily_sigma,
            },
        )
    ]


def evaluate(ctx: Context, enabled: set[str] | None = None) -> list[Trigger]:
    """Run every rule. One raising rule must not cost the others their turn."""
    import logging

    log = logging.getLogger(__name__)
    out: list[Trigger] = []
    for rid, r in REGISTRY.items():
        if enabled is not None and rid not in enabled:
            continue
        try:
            out.extend(r.predicate(ctx))
        except Exception:
            log.exception("rule %s raised; skipping it this tick", rid)
    return out
