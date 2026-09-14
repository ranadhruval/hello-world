"""Notepad, scheduler, incidents, voice, suggestions (B2, B6, B8, B9, B10)."""

from datetime import datetime, time, timedelta

import pytest

from app.compose.voice import VOICE, advice_violations, is_compliant
from app.config import IST
from app.obs.incidents import (
    REPAGE_AFTER,
    Incident,
    IncidentState,
    incident_id,
    should_page,
    signature,
)
from app.watcher.notepad import MAX_JOB_TOTAL_BYTES, MAX_VALUE_BYTES, NotepadFull, check_write
from app.watcher.schedule import (
    CATCH_UP_MAX_S,
    DailyScheduler,
    Job,
    SkipReason,
    catch_up_window_s,
    due,
    minutes_until,
)
from app.watcher.suggestions import (
    MAX_PENDING,
    REPEAT_THRESHOLD,
    Source,
    State,
    Suggestion,
    dedup_key,
    from_new_position,
    from_repeated_questions,
    may_propose,
    render,
)

TUE = datetime(2026, 9, 15, 9, 6, tzinfo=IST)  # a trading day


# ---- B2 notepad -----------------------------------------------------


def test_a_normal_cursor_write_is_fine():
    check_write("since", "cursor-abc123", {})


def test_an_oversized_value_is_refused():
    with pytest.raises(NotepadFull):
        check_write("k", "x" * (MAX_VALUE_BYTES + 1), {})


def test_the_job_total_is_capped_not_just_each_value():
    """A value that grows every tick is a job that works for a month and then
    stops, far from the cause."""
    existing = {f"k{i}": "x" * 4000 for i in range(16)}
    with pytest.raises(NotepadFull):
        check_write("more", "x" * 4000, existing)


def test_overwriting_a_key_does_not_double_count_it():
    existing = {"since": "x" * (MAX_JOB_TOTAL_BYTES - 100)}
    check_write("since", "y" * 50, existing)


def test_an_empty_or_overlong_key_is_refused():
    with pytest.raises(ValueError):
        check_write("", "v", {})
    with pytest.raises(ValueError):
        check_write("k" * 200, "v", {})


def test_byte_length_not_character_length():
    """Multi-byte characters must count as their encoded size."""
    with pytest.raises(NotepadFull):
        check_write("k", "₹" * MAX_VALUE_BYTES, {})


# ---- B6 scheduler ---------------------------------------------------


def job(at=time(9, 5), **kw):
    async def noop():
        return None

    return Job(name="brief", at=at, fn=kw.pop("fn", noop), **kw)


def test_a_job_fires_once_its_instant_passes():
    slot, reason = due(job(), TUE, None)
    assert slot is not None and reason == ""


def test_a_job_does_not_fire_before_its_instant():
    assert due(job(at=time(15, 45)), TUE, None)[1] == SkipReason.NOT_DUE


def test_a_job_does_not_fire_twice_in_one_day():
    assert due(job(), TUE, TUE.date())[1] == SkipReason.ALREADY_RAN


def test_a_restart_just_after_the_slot_still_fires():
    """09:07 should still send the 09:05 brief."""
    slot, _ = due(job(at=time(9, 5)), TUE.replace(hour=9, minute=7), None)
    assert slot is not None


def test_a_restart_hours_later_does_not_replay_the_day():
    assert due(job(at=time(9, 5)), TUE.replace(hour=14), None)[1] == SkipReason.TOO_LATE


def test_catch_up_off_means_a_late_restart_stays_quiet():
    """A restart at 16:30 must not re-send the 16:00 wrap."""
    late = TUE.replace(hour=16, minute=30)
    assert due(job(at=time(16, 0), catch_up=False), late, None)[1] == SkipReason.TOO_LATE


def test_the_catch_up_window_is_bounded():
    """Half the period, clamped — so a long outage cannot replay a day."""
    assert catch_up_window_s() == CATCH_UP_MAX_S
    assert catch_up_window_s(period_s=600) == 300


def test_trading_day_only_jobs_skip_the_weekend():
    sunday = datetime(2026, 9, 20, 9, 6, tzinfo=IST)
    assert due(job(), sunday, None)[1] == SkipReason.NOT_TRADING_DAY


def test_a_job_can_opt_out_of_the_trading_calendar():
    sunday = datetime(2026, 9, 20, 9, 6, tzinfo=IST)
    assert due(job(trading_days_only=False), sunday, None)[0] is not None


def test_minutes_until_rolls_to_tomorrow_once_past():
    assert minutes_until(job(at=time(9, 5)), TUE) == pytest.approx(24 * 60 - 1, abs=1)


class FakeSchedStore:
    def __init__(self, last=None, claimable=True):
        self.last = last or {}
        self.claimable = claimable
        self.claimed: list[tuple] = []
        self.finished: list[tuple] = []

    async def job_last_run(self, name):
        return self.last.get(name)

    async def claim_slot(self, name, instant):
        self.claimed.append((name, instant))
        return self.claimable

    async def finish_slot(self, name, instant, ok, error=None):
        self.finished.append((name, instant, ok, error))


async def test_the_slot_is_claimed_before_the_job_runs():
    """At-most-once across a mid-run crash: a crash loses the run rather than
    repeating it, which is the right way round for anything that sends."""
    order = []
    store = FakeSchedStore()

    async def record():
        order.append("ran")

    original_claim = store.claim_slot

    async def claim(name, instant):
        order.append("claimed")
        return await original_claim(name, instant)

    store.claim_slot = claim
    await DailyScheduler(store, [job(fn=record)], clock=lambda: TUE).tick()
    assert order == ["claimed", "ran"]


async def test_an_unclaimable_slot_is_not_run():
    store = FakeSchedStore(claimable=False)
    ran = await DailyScheduler(store, [job()], clock=lambda: TUE).tick()
    assert ran == [] and store.finished == []


async def test_a_raising_job_is_recorded_not_dropped():
    """A scheduler that quietly does nothing is indistinguishable from one
    working on a quiet day."""

    async def boom():
        raise RuntimeError("nope")

    store = FakeSchedStore()
    ran = await DailyScheduler(store, [job(fn=boom)], clock=lambda: TUE).tick()
    assert ran == []
    assert store.finished[0][2] is False and "nope" in store.finished[0][3]


async def test_a_raising_job_does_not_stop_the_others():
    async def boom():
        raise RuntimeError("nope")

    async def fine():
        return None

    store = FakeSchedStore()
    jobs = [Job("a", time(9, 5), boom), Job("b", time(9, 5), fine)]
    assert await DailyScheduler(store, jobs, clock=lambda: TUE).tick() == ["b"]


async def test_a_hanging_job_times_out_and_is_recorded():
    import asyncio

    async def hang():
        await asyncio.sleep(10)

    store = FakeSchedStore()
    await DailyScheduler(store, [job(fn=hang, timeout_s=0.01)], clock=lambda: TUE).tick()
    assert store.finished[0][2] is False and "timed out" in store.finished[0][3]


# ---- B8 incidents ---------------------------------------------------


def test_the_same_fault_with_different_ids_is_one_incident():
    a = "connection to 127.0.0.1:6379 failed at 2026-09-15T11:04:12 (attempt 3)"
    b = "connection to 127.0.0.1:6379 failed at 2026-09-15T14:22:01 (attempt 9)"
    assert incident_id("poll", a) == incident_id("poll", b)


def test_a_genuinely_different_fault_is_a_different_incident():
    assert incident_id("poll", "redis down") != incident_id("poll", "postgres down")


def test_the_same_error_in_a_different_job_is_a_different_incident():
    assert incident_id("poll", "boom") != incident_id("brief", "boom")


def test_the_signature_strips_what_varies():
    assert signature("failed at 2026-09-15T11:04:12 after 3 tries") == signature(
        "failed at 2026-09-16T09:00:00 after 91 tries"
    )


def inc(state, alerted_at=None, seen=None):
    return Incident("inc_1", "poll", "sig", state, 1, seen or datetime(2026, 9, 15), alerted_at)


def test_a_new_fault_pages():
    assert should_page(None, datetime(2026, 9, 15))


def test_an_already_paged_fault_stays_quiet():
    """A broken feed would otherwise produce hundreds of identical pages, and
    the operator learns to ignore them — the same failure the gate prevents."""
    now = datetime(2026, 9, 15, 12)
    assert not should_page(inc(IncidentState.ALERTED, alerted_at=now - timedelta(minutes=5)), now)


def test_a_long_quiet_fault_is_news_again():
    now = datetime(2026, 9, 15, 12)
    assert should_page(inc(IncidentState.ALERTED, alerted_at=now - REPAGE_AFTER), now)


def test_a_closed_fault_that_recurs_pages():
    """Closing it was a claim that it was fixed."""
    assert should_page(inc(IncidentState.CLOSED), datetime(2026, 9, 15, 12))


# ---- B9 voice -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "You should book profits here.",
        "I'd recommend trimming this position.",
        "My advice is to hold.",
        "Might be a good time to sell.",
        "You need to hedge this.",
        "Consider buying more.",
    ],
)
def test_advice_shaped_phrasing_is_blocked(text):
    """A compliance boundary, not a style preference: an unregistered system
    telling someone to buy or sell a specific security is a regulatory problem."""
    assert not is_compliant(text)
    assert advice_violations(text)


@pytest.mark.parametrize(
    "text",
    [
        "TITAN −3.2%. You hold 40 — ₹1,23,600, 35% of your book.",
        "Margin now tight — 72% used, was engaged.",
        "Spot is 0.7% through your strike with one session left.",
        "I don't know — that figure didn't come back from the broker.",
    ],
)
def test_observational_reporting_passes(text):
    assert is_compliant(text)


def test_the_voice_contract_states_the_load_bearing_rules():
    for rule in ("tool result", "Describe, do not prescribe", "don't know"):
        assert rule in VOICE


# ---- B10 suggestions ------------------------------------------------


def s(key="asked_repeatedly|TITAN", state=State.PENDING):
    return Suggestion(dedup_key=key, source=Source.ASKED_REPEATEDLY, reason="r", state=state)


def test_a_dismissed_suggestion_is_never_re_offered():
    """Re-offering a refusal is the fastest way to train someone to ignore you
    — and they ignore the good proposals with it."""
    ok, why = may_propose(s(), [s(state=State.DISMISSED)])
    assert not ok and why == "dismissed_before"


def test_the_same_suggestion_is_not_proposed_twice():
    assert may_propose(s(), [s()])[1] == "already_proposed"


def test_the_pending_list_is_capped():
    """A backlog of suggestions is not a feature; it is why someone mutes the
    whole channel."""
    existing = [s(key=f"k{i}") for i in range(MAX_PENDING)]
    assert may_propose(s(key="new"), existing)[1] == "pending_full"


def test_a_decided_suggestion_does_not_occupy_a_pending_slot():
    existing = [s(key=f"k{i}", state=State.ACCEPTED) for i in range(MAX_PENDING + 2)]
    assert may_propose(s(key="new"), existing)[0]


def test_a_fresh_proposal_is_allowed():
    assert may_propose(s(), [])[0]


def test_one_mention_does_not_earn_a_proposal():
    """Proposing on a single mention makes the desk feel like it is reading
    over your shoulder."""
    assert from_repeated_questions("TITAN", 1) is None


def test_repeated_questions_earn_a_proposal_in_the_users_terms():
    out = from_repeated_questions("titan", REPEAT_THRESHOLD)
    assert out is not None
    assert "TITAN" in out.reason and str(REPEAT_THRESHOLD) in out.reason
    assert out.spec["trading_symbol"] == "TITAN"


def test_a_new_position_earns_a_proposal():
    out = from_new_position("reliance", "CASH")
    assert out.source is Source.POSITION_OPENED
    assert out.dedup_key == dedup_key(Source.POSITION_OPENED, "RELIANCE")


def test_the_offer_renders_as_numbered_replies():
    """Baileys does not render buttons reliably on personal accounts."""
    out = render([s(), s(key="k2")])
    assert "  1  " in out and "  2  " in out and "Reply" in out


def test_no_pending_suggestions_renders_nothing():
    assert render([]) == ""
