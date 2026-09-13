"""Market-hours awareness (spec §5.6).

Outside hours: answer with the last close, label it, and suppress anything
time-sensitive. Segments differ — MCX runs to 23:30 while equity closes at
15:30, and a bot that treats them the same is wrong for half the day.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path

from app.config import IST


class SessionState(StrEnum):
    """What the market is doing right now.

    HOLIDAY is deliberately distinct from CLOSED: "shut for Diwali" and "it's
    Sunday" are different messages, and a scheduler that cannot tell them apart
    either fires on a holiday or stays silent on a trading day.
    """

    CLOSED = "closed"
    HOLIDAY = "holiday"
    PREOPEN = "preopen"
    OPEN = "open"
    POST_CLOSE = "post_close"


EQUITY_OPEN = time(9, 15)
EQUITY_CLOSE = time(15, 30)
PREOPEN_START = time(9, 0)
PREOPEN_END = time(9, 8)

MCX_OPEN = time(9, 0)
MCX_CLOSE_NON_AGRI = time(23, 30)
MCX_CLOSE_AGRI = time(17, 0)

HOLIDAYS_PATH = Path(__file__).parent / "holidays.json"

# MCX agri contracts close at 17:00, everything else at 23:30.
AGRI_UNDERLYINGS = frozenset(
    {"COTTON", "CPO", "KAPAS", "MENTHAOIL", "RUBBER", "CASTORSEED", "GUARGUM", "GUARSEED"}
)


class HolidaysMissing(RuntimeError):
    """The holiday file does not cover a year we are about to trade through."""


def _load_holidays() -> set[date]:
    """Read the holiday file. Keys that are not a four-digit year are notes."""
    if not HOLIDAYS_PATH.exists():
        return set()
    raw = json.loads(HOLIDAYS_PATH.read_text())
    out: set[date] = set()
    for year, entries in raw.items():
        if not (year.isdigit() and len(year) == 4):
            continue
        out.update(date.fromisoformat(d) for d in entries)
    return out


def _years_covered() -> set[int]:
    if not HOLIDAYS_PATH.exists():
        return set()
    raw = json.loads(HOLIDAYS_PATH.read_text())
    return {int(y) for y in raw if y.isdigit() and len(y) == 4}


# Keyed on the file's mtime, so an edit is picked up without a restart. The
# previous version memoised forever, which meant a watcher running for months
# never saw a holiday added — and was silently wrong about whether the market
# was open, which is the worst way to be wrong.
_HOLIDAYS: tuple[float, set[date]] | None = None


def holidays() -> set[date]:
    global _HOLIDAYS
    mtime = HOLIDAYS_PATH.stat().st_mtime if HOLIDAYS_PATH.exists() else 0.0
    if _HOLIDAYS is None or _HOLIDAYS[0] != mtime:
        _HOLIDAYS = (mtime, _load_holidays())
    return _HOLIDAYS[1]


def assert_holidays_cover(year: int) -> None:
    """Refuse to run a scheduler against a year we have no holiday data for.

    A missing file makes `holidays()` return an empty set, which reads as
    "every weekday is a trading day" — so the failure is silent and produces
    briefs on Diwali. Invariant I5: fail loudly instead.
    """
    covered = _years_covered()
    if year not in covered:
        raise HolidaysMissing(
            f"{HOLIDAYS_PATH} has no entry for {year} "
            f"(covers: {sorted(covered) or 'nothing'}). "
            "Populate it from the official NSE holiday list before running the watcher."
        )


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in holidays()


def now_ist() -> datetime:
    return datetime.now(IST)


def is_open(segment: str, at: datetime | None = None, underlying: str = "") -> bool:
    at = at or now_ist()
    if not is_trading_day(at.date()):
        return False
    t = at.time()
    if segment.upper() == "COMMODITY":
        close = MCX_CLOSE_AGRI if underlying.upper() in AGRI_UNDERLYINGS else MCX_CLOSE_NON_AGRI
        return MCX_OPEN <= t < close
    return EQUITY_OPEN <= t < EQUITY_CLOSE


def is_preopen(at: datetime | None = None) -> bool:
    at = at or now_ist()
    return is_trading_day(at.date()) and PREOPEN_START <= at.time() < PREOPEN_END


def previous_trading_day(d: date) -> date:
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def close_label(segment: str, at: datetime | None = None, underlying: str = "") -> str:
    """'at close (Fri 15:30)' — shown whenever the market is shut (spec §1.5)."""
    at = at or now_ist()
    if is_open(segment, at, underlying):
        return ""
    if segment.upper() == "COMMODITY":
        close = MCX_CLOSE_AGRI if underlying.upper() in AGRI_UNDERLYINGS else MCX_CLOSE_NON_AGRI
    else:
        close = EQUITY_CLOSE
    day = (
        at.date()
        if (is_trading_day(at.date()) and at.time() >= close)
        else previous_trading_day(at.date())
    )
    return f"at close ({day:%a} {close:%H:%M})"


def _close_time(segment: str, underlying: str = "") -> time:
    if segment.upper() == "COMMODITY":
        return MCX_CLOSE_AGRI if underlying.upper() in AGRI_UNDERLYINGS else MCX_CLOSE_NON_AGRI
    return EQUITY_CLOSE


def _open_time(segment: str) -> time:
    return MCX_OPEN if segment.upper() == "COMMODITY" else EQUITY_OPEN


def session_bounds(segment: str, d: date, underlying: str = "") -> tuple[datetime, datetime]:
    """Open and close instants for `d`, whether or not it is a trading day."""
    return (
        datetime.combine(d, _open_time(segment), tzinfo=IST),
        datetime.combine(d, _close_time(segment, underlying), tzinfo=IST),
    )


def session_state(segment: str, at: datetime | None = None, underlying: str = "") -> SessionState:
    """One call the watcher switches on, instead of chaining is_open/is_preopen.

    Every polling loop and every scheduled job is gated on this. Getting it
    wrong in the quiet direction means missed alerts; in the loud direction it
    means messaging people on a holiday.
    """
    at = at or now_ist()
    if at.date() in holidays():
        return SessionState.HOLIDAY
    if at.weekday() >= 5:
        return SessionState.CLOSED
    if is_preopen(at) and segment.upper() != "COMMODITY":
        return SessionState.PREOPEN
    if is_open(segment, at, underlying):
        return SessionState.OPEN
    if at.time() >= _close_time(segment, underlying):
        return SessionState.POST_CLOSE
    return SessionState.CLOSED


def next_open(segment: str, at: datetime | None = None, underlying: str = "") -> datetime:
    """The next instant this segment opens.

    Lets a loop sleep until the open instead of ticking all night, and lets the
    EOD digest say "next session Monday" rather than going quiet.
    """
    at = at or now_ist()
    opens = _open_time(segment)
    day = at.date()
    if not (is_trading_day(day) and at.time() < opens):
        day += timedelta(days=1)
        while not is_trading_day(day):
            day += timedelta(days=1)
    return datetime.combine(day, opens, tzinfo=IST)


def minutes_to_close(segment: str, at: datetime | None = None, underlying: str = "") -> int | None:
    """Minutes until this segment closes, or None when it is not open.

    The gate uses this for time-criticality: an expiry alert at 15:25 is worth
    more than the same alert at 10:00, and low-urgency noise in the last few
    minutes is worth less than at any other time.
    """
    at = at or now_ist()
    if not is_open(segment, at, underlying):
        return None
    close = datetime.combine(at.date(), _close_time(segment, underlying), tzinfo=IST)
    return max(0, int((close - at).total_seconds() // 60))
