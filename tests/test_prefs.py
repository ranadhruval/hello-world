"""Preference commands (S2).

A dogfooder who cannot turn the briefs down turns them off, and takes the
useful messages with them. These are switches, so they must be exact: "pause"
means pause on the first try, offline, with no chance of being read as a
question about the market.
"""

from datetime import UTC, datetime, time

import pytest

from app.render.prefs import confirm, settings
from app.router.prefs import QUIET_TODAY_SENTINEL, PrefAction, parse


@pytest.mark.parametrize(
    "text",
    [
        "pause",
        "Pause",
        "  pause  ",
        "pause briefs",
        "stop briefs",
        "mute alerts",
        "pause everything",
    ],
)
def test_pause_is_recognised(text):
    assert parse(text).action is PrefAction.PAUSE


@pytest.mark.parametrize("text", ["resume", "unpause", "start briefs", "unmute"])
def test_resume_is_recognised(text):
    assert parse(text).action is PrefAction.RESUME


@pytest.mark.parametrize(
    "text,minutes",
    [
        ("snooze 2h", 120),
        ("snooze 30m", 30),
        ("quiet 90 minutes", 90),
        ("mute for 3 hours", 180),
        ("snooze 1hr", 60),
    ],
)
def test_snooze_durations(text, minutes):
    cmd = parse(text)
    assert cmd.action is PrefAction.SNOOZE and cmd.minutes == minutes


def test_quiet_today_is_its_own_thing():
    """'Today' to someone texting at 11pm means until tomorrow, not an hour."""
    assert parse("quiet today").minutes == QUIET_TODAY_SENTINEL


@pytest.mark.parametrize(
    "text,at,which",
    [
        ("brief at 8:15", time(8, 15), "pre_market"),
        ("morning brief at 8am", time(8, 0), "pre_market"),
        ("pre-market brief at 7:30", time(7, 30), "pre_market"),
        ("wrap at 4pm", time(16, 0), "post_close"),
        ("evening brief at 6pm", time(18, 0), "post_close"),
        ("eod at 16:10", time(16, 10), "post_close"),
    ],
)
def test_brief_times(text, at, which):
    cmd = parse(text)
    assert cmd.action is PrefAction.SET_BRIEF_TIME
    assert cmd.at == at and cmd.which == which


def test_a_bare_afternoon_hour_is_read_as_afternoon():
    """Nobody schedules a market wrap at 4am, and guessing wrong puts the
    message at the opposite end of the day."""
    assert parse("wrap at 4").at == time(16, 0)


def test_midnight_and_noon_meridiems():
    assert parse("brief at 12am").at == time(0, 0)
    assert parse("brief at 12pm").at == time(12, 0)


def test_an_impossible_time_is_not_a_command():
    assert parse("brief at 99:99") is None


@pytest.mark.parametrize(
    "text",
    [
        "portfolio",
        "nifty",
        "margin",
        "what's my pnl",
        "should I pause my SIP",  # contains 'pause', is a question
        "mute gold",  # entity mute, a different grammar
        "how do I stop losing money",
    ],
)
def test_ordinary_messages_are_left_alone(text):
    """The parser runs before classification, so a false positive here answers
    a market question with a settings confirmation."""
    assert parse(text) is None


def test_empty_is_not_a_command():
    assert parse("") is None and parse("   ") is None


# ---- replies --------------------------------------------------------


def test_pause_says_how_to_undo_it():
    out = confirm(parse("pause"), {})
    assert "resume" in out.lower()


def test_resume_states_the_times_it_resumed_to():
    out = confirm(parse("resume"), {"brief_pre_market": time(8, 15)})
    assert "08:15" in out


def test_settings_shows_defaults_when_nothing_is_set():
    out = settings({})
    assert "08:45" in out and "15:45" in out and "active" in out


def test_settings_shows_paused():
    assert "paused" in settings({"briefs_paused": True})


def test_settings_tells_you_the_grammar():
    assert "snooze" in settings({})


def test_a_snooze_time_reads_the_same_in_both_places():
    """Postgres returns timestamps in the server's zone. Rendering one raw put
    'quiet until 05:12' in settings next to a confirmation saying 10:42 — the
    same instant shown two ways, which reads as the desk being confused."""

    utc = datetime(2026, 9, 15, 5, 12, tzinfo=UTC)  # 10:42 IST
    assert "10:42" in confirm(parse("snooze 2h"), {}, until=utc)
    assert "10:42" in settings({"muted_until": utc})
