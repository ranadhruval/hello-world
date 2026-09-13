"""Market-hours awareness (spec §5.6).

Outside hours: answer with the last close, label it, and suppress anything
time-sensitive. Segments differ — MCX runs to 23:30 while equity closes at
15:30, and a bot that treats them the same is wrong for half the day.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time
from pathlib import Path

from app.config import IST

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


_HOLIDAYS: set[date] | None = None


def holidays() -> set[date]:
    global _HOLIDAYS
    if _HOLIDAYS is None:
        _HOLIDAYS = _load_holidays()
    return _HOLIDAYS


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
    from datetime import timedelta

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
    day = at.date() if (is_trading_day(at.date()) and at.time() >= close) else previous_trading_day(at.date())
    return f"at close ({day:%a} {close:%H:%M})"
