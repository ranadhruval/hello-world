"""Preference commands, parsed before anything else (S2).

A dogfooder who cannot turn the briefs down will turn them off. The benchmark's
routine task scores "easy to edit or pause" as a third of the mark, and it is
right to: a scheduled message you cannot move is a scheduled message you
eventually mute, and muting takes the useful ones with it.

Deliberately a small closed grammar rather than a model call. These are control
commands — "pause" must mean pause on the first try, every time, offline, with
no token spend and no chance of being read as a question about the market. The
model is for questions; this is a switch.

Parsed before `classify()` because the fast-path gauntlet would otherwise send
"pause" down the instrument resolver and answer with a stock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from enum import StrEnum


class PrefAction(StrEnum):
    PAUSE = "pause"  # stop briefs and alerts until resumed
    RESUME = "resume"
    SNOOZE = "snooze"  # quiet for a bounded stretch
    SET_BRIEF_TIME = "set_brief_time"
    SHOW = "show"


@dataclass(frozen=True)
class PrefCommand:
    action: PrefAction
    minutes: int | None = None  # SNOOZE
    at: time | None = None  # SET_BRIEF_TIME
    which: str = ""  # "pre_market" | "post_close"


# Bare, unambiguous switches. Anchored so "pause" matches and "should I pause
# my SIP" does not.
_PAUSE = re.compile(
    r"^\s*(?:pause|stop|mute)(?:\s+(?:the\s+)?(?:briefs?|alerts?|everything))?\s*$", re.I
)
_RESUME = re.compile(
    r"^\s*(?:resume|unpause|start|unmute)(?:\s+(?:the\s+)?(?:briefs?|alerts?|everything))?\s*$",
    re.I,
)
_SHOW = re.compile(r"^\s*(?:settings|prefs|preferences)\s*$", re.I)

# "snooze 2h", "quiet 30m", "mute for 90 minutes"
_SNOOZE = re.compile(
    r"^\s*(?:snooze|quiet|mute)(?:\s+for)?\s+(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*$",
    re.I,
)
_QUIET_TODAY = re.compile(r"^\s*quiet\s+(?:today|for\s+today)\s*$", re.I)

# "brief at 8:15", "morning brief at 8am", "wrap at 4pm"
_AT_TIME = re.compile(
    r"^\s*(?:(morning|pre[- ]?market|evening|wrap|post[- ]?close|eod)\s+)?"
    r"(?:brief|wrap|digest|summary)?\s*(?:at|@)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$",
    re.I,
)

_EVENING = {"evening", "wrap", "post-close", "post close", "postclose", "eod"}

# Quiet-for-today ends at the next morning's brief window rather than midnight —
# "today" to someone texting at 11pm means "until tomorrow", not "for an hour".
QUIET_TODAY_UNTIL = time(8, 0)
QUIET_TODAY_SENTINEL = -1


def _minutes(value: str, unit: str) -> int:
    n = int(value)
    return n * 60 if unit.lower().startswith("h") else n


def parse(text: str) -> PrefCommand | None:
    """A preference command, or None to let normal routing handle it."""
    raw = (text or "").strip()
    if not raw:
        return None

    if _PAUSE.match(raw):
        return PrefCommand(PrefAction.PAUSE)
    if _RESUME.match(raw):
        return PrefCommand(PrefAction.RESUME)
    if _SHOW.match(raw):
        return PrefCommand(PrefAction.SHOW)
    if _QUIET_TODAY.match(raw):
        return PrefCommand(PrefAction.SNOOZE, minutes=QUIET_TODAY_SENTINEL)

    m = _SNOOZE.match(raw)
    if m:
        return PrefCommand(PrefAction.SNOOZE, minutes=_minutes(m.group(1), m.group(2)))

    m = _AT_TIME.match(raw)
    if m:
        label, hh, mm, meridiem = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        if meridiem:
            lower = meridiem.lower()
            if lower == "pm" and hh != 12:
                hh += 12
            elif lower == "am" and hh == 12:
                hh = 0
        elif hh < 7:
            # "wrap at 4" means 16:00 — nobody schedules a market brief at 4am,
            # and guessing wrong here is a message at the wrong end of the day.
            hh += 12
        if not (0 <= hh <= 23) or (mm and not 0 <= int(mm) <= 59):
            return None
        which = "post_close" if (label or "").lower().strip() in _EVENING else "pre_market"
        return PrefCommand(PrefAction.SET_BRIEF_TIME, at=time(hh, int(mm or 0)), which=which)

    return None
