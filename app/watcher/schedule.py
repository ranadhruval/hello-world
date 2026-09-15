"""IST-aware, market-hours-aware daily scheduler (B6).

Every invariant here guards a named failure. None is theoretical.

**Short sleeps, never long ones.** The instrument refresh this replaced
computes its target once and then awaits a single sleep of up to 24 hours. A
laptop suspend or an NTP step silently breaks that, and the runbook already
lists "bot stops overnight / your Mac slept" as a known limitation. Ticking
every 20 seconds is immune to both.

**Advance before dispatch.** The next run instant is recorded *before* the job
runs, and the slot ledger's unique key blocks a second fire. A crash mid-run
therefore loses the run, never repeats it — the right way round for anything
that sends a message.

**Never drop a slot silently.** A slot that is skipped is written down with the
reason. A scheduler that quietly does nothing is indistinguishable from a
scheduler that is working on a quiet day, which is precisely the failure we
cannot afford.

**Catch-up is bounded.** A restart at 09:07 should still send the 09:05 brief;
a restart at 16:30 must not re-send the 16:00 wrap. The window is half the
period, clamped to 2 minutes–2 hours.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app.config import IST
from app.market.calendar import is_trading_day, now_ist

log = logging.getLogger(__name__)

TICK_S = 20
CATCH_UP_MIN_S = 120
CATCH_UP_MAX_S = 7200
DAILY_PERIOD_S = 86_400


class SkipReason:
    NOT_TRADING_DAY = "not_a_trading_day"
    ALREADY_RAN = "already_ran_today"
    TOO_LATE = "outside_catch_up_window"
    NOT_DUE = "not_due"


def catch_up_window_s(period_s: int = DAILY_PERIOD_S) -> float:
    """Half the period, clamped. Bounded so a long outage cannot replay a day."""
    return min(max(period_s / 2, CATCH_UP_MIN_S), CATCH_UP_MAX_S)


@dataclass(frozen=True)
class Job:
    name: str
    at: time
    fn: Callable[[], Awaitable[None]]
    trading_days_only: bool = True
    catch_up: bool = True
    timeout_s: float = 120.0

    def instant_on(self, day: date) -> datetime:
        return datetime.combine(day, self.at, tzinfo=IST)


def due(job: Job, now: datetime, last_run_on: date | None) -> tuple[datetime | None, str]:
    """The slot this job should fire for, or None and why not.

    Pure: the whole point is that "did the 09:05 brief already go out today"
    can be answered in a test rather than at 09:05.
    """
    today = now.date()
    if job.trading_days_only and not is_trading_day(today):
        return None, SkipReason.NOT_TRADING_DAY
    if last_run_on == today:
        return None, SkipReason.ALREADY_RAN

    slot = job.instant_on(today)
    if now < slot:
        return None, SkipReason.NOT_DUE

    late_by = (now - slot).total_seconds()
    if not job.catch_up and late_by > CATCH_UP_MIN_S:
        return None, SkipReason.TOO_LATE
    if late_by > catch_up_window_s():
        return None, SkipReason.TOO_LATE
    return slot, ""


class DailyScheduler:
    """Runs jobs at IST wall-clock times, at most once per day each.

    `store` supplies the durable last-run state. Postgres and not Redis on
    purpose: a Redis flush must never cause a duplicate end-of-day digest to
    every user. Redis is the doorbell; Postgres is the truth.
    """

    def __init__(self, store, jobs: list[Job] | None = None, clock=now_ist) -> None:
        self._store = store
        self._jobs: list[Job] = list(jobs or [])
        self._clock = clock

    def add(self, job: Job) -> None:
        self._jobs.append(job)

    async def tick(self) -> list[str]:
        """One pass. Returns the names of the jobs that ran."""
        now = self._clock()
        ran: list[str] = []
        for job in self._jobs:
            last = await self._store.job_last_run(job.name)
            slot, reason = due(job, now, last)
            if slot is None:
                if reason not in (SkipReason.NOT_DUE, SkipReason.ALREADY_RAN):
                    log.info("job %s skipped: %s", job.name, reason)
                continue

            # Claim the slot BEFORE running it. The unique key on
            # (job, instant) is what makes this at-most-once across a crash.
            if not await self._store.claim_slot(job.name, slot):
                log.info("job %s slot %s already claimed", job.name, slot)
                continue

            ok, error = True, None
            try:
                await asyncio.wait_for(job.fn(), timeout=job.timeout_s)
            except TimeoutError:
                ok, error = False, f"timed out after {job.timeout_s}s"
                log.error("job %s %s", job.name, error)
            except Exception as exc:
                ok, error = False, f"{type(exc).__name__}: {exc}"
                log.exception("job %s raised", job.name)
            else:
                ran.append(job.name)

            # Recorded whatever happened — a slot is never dropped silently.
            await self._store.finish_slot(job.name, slot, ok=ok, error=error)
        return ran

    async def run(self) -> None:  # pragma: no cover - process entrypoint
        log.info("scheduler up with %d jobs", len(self._jobs))
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed; continuing")
            await asyncio.sleep(TICK_S)


def minutes_until(job: Job, now: datetime) -> int:
    """For diagnostics: how long until this job's next slot."""
    slot = job.instant_on(now.date())
    if now >= slot:
        slot = job.instant_on(now.date() + timedelta(days=1))
    return int((slot - now).total_seconds() // 60)
