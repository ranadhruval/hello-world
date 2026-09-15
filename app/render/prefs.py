"""Replies to preference commands (S2).

Short and literal. A control command's reply exists to confirm exactly what
changed, so the next thing the user thinks is not "did that work".
"""

from __future__ import annotations

from datetime import datetime, time

from app.config import IST
from app.router.prefs import PrefAction, PrefCommand


def _ist(ts: datetime | None) -> datetime | None:
    """Postgres hands timestamps back in the server's zone. Rendering one raw
    showed 'quiet until 05:12' next to a confirmation saying 10:42 — the same
    instant, two different times, which reads as the desk being confused."""
    if ts is None:
        return None
    return ts.astimezone(IST) if ts.tzinfo else ts.replace(tzinfo=IST)


def _hhmm(t: time | None, fallback: str) -> str:
    return f"{t:%H:%M}" if t else fallback


def confirm(cmd: PrefCommand, prefs: dict, *, until: datetime | None = None) -> str:
    if cmd.action is PrefAction.PAUSE:
        return 'Paused. No briefs or alerts until you say "resume".'

    if cmd.action is PrefAction.RESUME:
        return (
            f"Resumed. Morning brief {_hhmm(prefs.get('brief_pre_market'), '08:45')}, "
            f"wrap {_hhmm(prefs.get('brief_post_close'), '15:45')}."
        )

    if cmd.action is PrefAction.SNOOZE:
        local = _ist(until)
        if local is None:
            return "Quiet for now."
        return f"Quiet until {local:%H:%M}. Anything urgent still gets through."

    if cmd.action is PrefAction.SET_BRIEF_TIME:
        which = "Morning brief" if cmd.which == "pre_market" else "Wrap"
        return f"{which} moved to {cmd.at:%H:%M}."

    return settings(prefs)


def settings(prefs: dict) -> str:
    """What the desk currently believes about how you want to be contacted."""
    paused = prefs.get("briefs_paused")
    until = _ist(prefs.get("muted_until"))
    lines = [
        f"Morning brief   {_hhmm(prefs.get('brief_pre_market'), '08:45')}",
        f"Wrap            {_hhmm(prefs.get('brief_post_close'), '15:45')}",
        f"Quiet hours     {_hhmm(prefs.get('quiet_start'), '23:30')}"
        f"–{_hhmm(prefs.get('quiet_end'), '08:00')}",
    ]
    if paused:
        lines.append("Status          paused")
    elif until:
        lines.append(f"Status          quiet until {until:%d %b %H:%M}")
    else:
        lines.append("Status          active")
    lines += ["", 'Say "pause", "snooze 2h", or "brief at 8:15".']
    return "\n".join(lines)
