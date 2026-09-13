"""The canonical inbound signal (docs/SIGNAL_CONTRACT.md).

Market-side signals come from an external insight engine. This module is the
only place that knows what they look like on the wire: adapters normalise a
vendor payload into `Signal`, and everything downstream — rules, gate,
composer — sees only this shape.

The invariant this module exists to enforce is contract §2 principle 1,
**missing is a value**. A source that cannot populate a field must not have it
silently become zero, because a plausible wrong number costs the user's trust
permanently while an absent one costs a single suppressed alert. So there is
no `payload.get(key, 0)` anywhere downstream: `number()` returns None and
`require()` raises, and neither can be mistaken for a real reading.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.config import IST


class SignalKind(StrEnum):
    MOVER = "market.mover"
    VOLUME_SPIKE = "market.volume_spike"
    BREAKOUT = "market.breakout"
    OI = "market.oi"
    MACRO = "market.macro"
    NEWS = "news.item"
    ANNOUNCEMENT = "filing.announcement"
    CORP_ACTION = "filing.corp_action"
    RESULTS = "filing.results_calendar"
    BLOCK_DEAL = "filing.block_deal"
    FNO_BAN = "filing.fno_ban"


# How long a signal of each kind stays worth acting on. A move explained
# twenty minutes late has usually already been explained by the market, and a
# stale alert reads as the system being asleep rather than watchful.
MAX_AGE_S: dict[SignalKind, int] = {
    SignalKind.VOLUME_SPIKE: 15 * 60,
    SignalKind.BREAKOUT: 15 * 60,
    SignalKind.MOVER: 15 * 60,
    SignalKind.OI: 30 * 60,
    SignalKind.NEWS: 6 * 3600,
    SignalKind.ANNOUNCEMENT: 6 * 3600,
    SignalKind.BLOCK_DEAL: 12 * 3600,
    SignalKind.MACRO: 3600,
    SignalKind.CORP_ACTION: 7 * 86_400,
    SignalKind.RESULTS: 14 * 86_400,
    SignalKind.FNO_BAN: 86_400,
}
DEFAULT_MAX_AGE_S = 3600


class MissingField(KeyError):
    """A signal did not carry a field we were about to build a claim on."""


@dataclass(frozen=True)
class Entity:
    """Who the signal is about, as the source identifies them.

    We resolve to our own instrument master rather than asking the source for
    ISIN, so this carries whatever identifiers it has. `exchange` and `segment`
    are required by the contract because a bare symbol is ambiguous across
    venues and a fuzzy matcher should not have to guess.
    """

    entity_id: str = ""
    exchange: str = ""
    segment: str = ""
    nse_symbol: str = ""
    bse_code: str = ""
    trading_symbol: str = ""
    underlying: str = ""

    @property
    def symbol(self) -> str:
        return self.trading_symbol or self.nse_symbol or self.bse_code

    @property
    def query(self) -> str:
        """What to hand the resolver."""
        return self.symbol or self.underlying

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.__dict__.items() if v}


@dataclass(frozen=True)
class Signal:
    source: str
    source_event_id: str
    kind: SignalKind
    entity: Entity
    observed_at: datetime
    payload: dict = field(default_factory=dict)
    event_at: datetime | None = None
    evidence: dict = field(default_factory=dict)
    absent: tuple[str, ...] = ()
    method: dict = field(default_factory=dict)

    # -- time ---------------------------------------------------------

    @property
    def at(self) -> datetime:
        """When it happened, falling back to when it was seen.

        For news these differ by a lot: the engine applies a seven-day recency
        window, so a row created today can describe something from Tuesday.
        """
        return self.event_at or self.observed_at

    def age_s(self, now: datetime | None = None) -> float:
        return ((now or datetime.now(IST)) - self.at).total_seconds()

    def is_stale(self, now: datetime | None = None) -> bool:
        return self.age_s(now) > MAX_AGE_S.get(self.kind, DEFAULT_MAX_AGE_S)

    # -- field access -------------------------------------------------

    def has(self, key: str) -> bool:
        return key not in self.absent and self.payload.get(key) is not None

    def number(self, key: str) -> float | None:
        """A numeric field, or None. Never a default, never a zero.

        Callers must branch on None. This is deliberately awkward: the easy
        version of this function is what puts a fabricated figure in front of
        a user.
        """
        if not self.has(key):
            return None
        try:
            return float(self.payload[key])
        except (TypeError, ValueError):
            return None

    def require(self, key: str) -> float:
        """A numeric field we refuse to proceed without."""
        v = self.number(key)
        if v is None:
            raise MissingField(f"{self.kind} from {self.source} has no usable {key!r}")
        return v

    def text(self, key: str, default: str = "") -> str:
        v = self.payload.get(key)
        return str(v) if v not in (None, "") and key not in self.absent else default

    def enum(self, key: str) -> str | None:
        v = self.text(key)
        return v.lower() or None


def _dt(v: object) -> datetime | None:
    """RFC 3339 in, tz-aware IST out. Naive timestamps are assumed IST."""
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt.astimezone(IST) if dt.tzinfo else dt.replace(tzinfo=IST)


def from_wire(d: dict, *, source: str = "") -> Signal:
    """Build a Signal from a contract-shaped payload.

    Unknown fields are ignored so the source can add them without a version
    bump, per contract §8.
    """
    ent = d.get("entity") or {}
    observed = _dt(d.get("observed_at")) or datetime.now(IST)
    return Signal(
        source=source or str(d.get("source", "unknown")),
        source_event_id=str(d.get("source_event_id") or d.get("signal_id") or ""),
        kind=SignalKind(d["kind"]),
        entity=Entity(
            entity_id=str(ent.get("entity_id", "")),
            exchange=str(ent.get("exchange", "")).upper(),
            segment=str(ent.get("segment", "")).upper(),
            nse_symbol=str(ent.get("nse_symbol", "")).upper(),
            bse_code=str(ent.get("bse_code", "")),
            trading_symbol=str(ent.get("trading_symbol", "")).upper(),
            underlying=str(ent.get("underlying", "")).upper(),
        ),
        observed_at=observed,
        event_at=_dt(d.get("event_at")),
        payload=dict(d.get("payload") or {}),
        evidence=dict(d.get("evidence") or {}),
        absent=tuple(d.get("absent") or ()),
        method=dict(d.get("method") or {}),
    )
