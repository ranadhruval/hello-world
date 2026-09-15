"""The interruption gate (spec §16).

The quality of this system is measured by its silence. An alert engine that
fires on every threshold crossing is a stock ticker with extra steps, and the
moment alerts become background noise the product is dead — no amount of
additional rules revives it. So this module's job is mostly to say no.

Scoring is multiplicative across seven stages and every stage is recorded, not
just the total. The trace is what makes offline tuning possible: when a week of
shadow mode produces an alert you would not have wanted, you need to see which
stage let it through, not merely that the number was 0.78.

Nothing here calls a model (invariant I2). Ranking is arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum

from app.watcher.exposure import ACCOUNT, Book
from app.watcher.rules import REGISTRY, Family, Trigger


class Route(StrEnum):
    INTERRUPT = "interrupt"  # now, its own message
    BATCH = "batch"  # held briefly, combined
    DIGEST = "digest"  # end of day
    SILENT = "silent"  # ledger only


# Spec §16.1 stage 4. Margin and expiry are irreversible — miss them and money
# is gone. A volume spike is information; you can always read it later.
IRREVERSIBILITY: dict[Family, float] = {
    Family.MARGIN: 1.4,
    Family.EXPIRY: 1.4,
    Family.STRUCTURE: 1.3,
    Family.PNL: 1.0,
    Family.CORP: 0.9,
    Family.NEWS: 0.8,
    Family.MARKET: 0.6,
}

# Families we never learn our way out of. Someone who ignores margin warnings
# is the last person whose margin warnings should be suppressed, so the learned
# weight is floored rather than allowed to decay to nothing. This is the one
# place the learning loop is deliberately broken — written down so future
# maintenance does not "fix" it.
PROTECTIVE = frozenset({Family.MARGIN, Family.EXPIRY, Family.STRUCTURE})
PROTECTIVE_FLOOR = 0.5

INTERRUPT_AT = 0.75
BATCH_AT = 0.45
DIGEST_AT = 0.20

P0_SEVERITY = 0.9  # always sends, ignores budget, ignores quiet hours

# A maximal instance of an irreversible family is P0 regardless of the rule's
# base severity. Without this, margin.band_up (base 0.85) could never qualify,
# so a *critical* margin band arriving in quiet hours was deferred to the
# morning digest — by which time the position may have been liquidated. Found
# by tests/test_restraint.py on its first run, which is what that file is for.
#
# Deliberately narrow: the magnitude must be maximal, which for margin means
# the critical band alone (stressed scores 0.85 and still waits its turn).
P0_PROTECTIVE_MAGNITUDE = 0.9
DEFAULT_BUDGET = 6  # interrupts per day
FATIGUE_PER_SENT = 0.12
FATIGUE_FLOOR = 0.3
DEFAULT_RESPONSIVENESS = 0.7

# Signal confluence. Two independent rules firing on the same name in the same
# window is materially more informative than either alone — unusual volume is a
# question, unusual volume plus a filing is an answer. This is how an analyst
# reads a tape, and without it each half sits below the bar and the pair is
# never noticed. Keyed on distinct rule ids, so repeated firings of one rule
# earn nothing.
CONFLUENCE = {1: 1.0, 2: 1.25}
CONFLUENCE_MAX = 1.5


@dataclass
class GateState:
    """Everything the gate reasons about besides the trigger itself."""

    now: datetime
    book: Book
    sent_today: int = 0
    budget: int = DEFAULT_BUDGET
    quiet_start: time = time(23, 30)
    quiet_end: time = time(8, 0)
    muted_rules: frozenset[str] = frozenset()
    muted_entities: frozenset[str] = frozenset()
    cooldown_until: dict[str, datetime] = field(default_factory=dict)
    beliefs: dict[str, float] = field(default_factory=dict)
    minutes_to_close: int | None = None
    # entity -> distinct rule ids that fired on it in the current window
    confluence: dict[str, set[str]] = field(default_factory=dict)

    def confluence_factor(self, entity: str, rule_id: str) -> float:
        # Account-level events are not "the same name": margin, day P&L and an
        # expiry all land on ACCOUNT, and treating that as corroboration would
        # let unrelated events amplify each other into an interrupt.
        if entity.upper() == ACCOUNT:
            return 1.0
        rules = set(self.confluence.get(entity, set()))
        rules.add(rule_id)
        return CONFLUENCE.get(len(rules), CONFLUENCE_MAX)

    def commit(self, trigger: Trigger, decision: Decision) -> None:
        """Record that a decision was acted on.

        Starts the rule's cooldown and, for an interrupt, spends a unit of the
        daily budget. Without this the same sustained condition re-fires on
        every tick: the dedupe key alone identifies the condition, it does not
        remember that we already spoke about it.
        """
        r = REGISTRY.get(trigger.rule_id)
        if r is not None and r.cooldown_s:
            self.cooldown_until[trigger.dedupe] = self.now + timedelta(seconds=r.cooldown_s)
        self.confluence.setdefault(trigger.entity, set()).add(trigger.rule_id)
        if decision.route is Route.INTERRUPT:
            self.sent_today += 1

    def in_quiet_hours(self) -> bool:
        t = self.now.time()
        if self.quiet_start <= self.quiet_end:
            return self.quiet_start <= t < self.quiet_end
        return t >= self.quiet_start or t < self.quiet_end  # wraps midnight

    def cooldown_active(self, dedupe: str) -> bool:
        until = self.cooldown_until.get(dedupe)
        return until is not None and self.now < until

    def budget_exhausted(self) -> bool:
        return self.sent_today >= self.budget


@dataclass(frozen=True)
class Decision:
    route: Route
    score: float
    trace: tuple[tuple[str, float], ...]
    reason: str = ""

    @property
    def sends(self) -> bool:
        return self.route in (Route.INTERRUPT, Route.BATCH)

    def explain(self) -> str:
        steps = " → ".join(f"{name} {val:.2f}" for name, val in self.trace)
        tail = f"  [{self.reason}]" if self.reason else ""
        return f"{self.route} {self.score:.2f}  ({steps}){tail}"


def _time_criticality(family: Family, minutes_to_close: int | None) -> float:
    """An expiry alert at 15:25 is worth more than the same alert at 10:00."""
    if minutes_to_close is None:
        return 1.0
    if family is Family.EXPIRY:
        if minutes_to_close <= 30:
            return 1.5
        if minutes_to_close <= 90:
            return 1.25
    if family is Family.MARGIN and minutes_to_close <= 60:
        return 1.3
    # Low-urgency noise in the last minutes of a session is worth less: there
    # is no longer time to act on it, so it belongs in the wrap.
    if family in (Family.MARKET, Family.NEWS) and minutes_to_close <= 5:
        return 0.7
    return 1.0


def score(trigger: Trigger, state: GateState) -> Decision:
    """Scoring stages, then routing. Every stage lands in the trace."""
    r = REGISTRY[trigger.rule_id]
    trace: list[tuple[str, float]] = []

    # 1 — rule severity, scaled by how strongly this instance fired
    s = r.base_severity * (0.5 + 0.5 * trigger.magnitude)
    trace.append(("severity", s))

    # 2 — materiality: is this big FOR THIS USER
    s *= state.book.materiality(trigger.entity)
    trace.append(("materiality", s))

    # 3 — time criticality
    s *= _time_criticality(r.family, state.minutes_to_close)
    trace.append(("timing", s))

    # 4 — irreversibility of the family
    s *= IRREVERSIBILITY.get(r.family, 1.0)
    trace.append(("irreversibility", s))

    # 5 — confluence with other rules on the same name
    s *= state.confluence_factor(trigger.entity, trigger.rule_id)
    trace.append(("confluence", s))

    # 6 — learned responsiveness, floored for protective families
    resp = state.beliefs.get(f"response.{r.family}", DEFAULT_RESPONSIVENESS)
    if r.family in PROTECTIVE:
        resp = max(resp, PROTECTIVE_FLOOR)
    s *= resp
    trace.append(("responsiveness", s))

    # 7 — fatigue: the fifth message of the day is worth less than the first
    s *= max(FATIGUE_FLOOR, 1.0 - FATIGUE_PER_SENT * state.sent_today)
    trace.append(("fatigue", s))

    frozen = tuple(trace)
    p0 = (r.base_severity >= P0_SEVERITY and trigger.magnitude >= 0.9) or (
        r.family in PROTECTIVE and trigger.magnitude >= P0_PROTECTIVE_MAGNITUDE
    )

    # 8 — hard suppressors. Order matters: an explicit mute beats everything
    # except a P0, and a cooldown beats the score entirely.
    if trigger.rule_id in state.muted_rules or trigger.entity in state.muted_entities:
        if not p0:
            return Decision(Route.SILENT, s, frozen, "muted")
    if state.cooldown_active(trigger.dedupe):
        return Decision(Route.SILENT, s, frozen, "cooldown")
    if state.in_quiet_hours() and not p0:
        return Decision(Route.DIGEST, s, frozen, "quiet_hours")
    if state.budget_exhausted() and not p0:
        return Decision(Route.BATCH, s, frozen, "budget_exhausted")

    if p0:
        return Decision(Route.INTERRUPT, s, frozen, "p0_override")

    # 9 — routing
    if s >= INTERRUPT_AT:
        return Decision(Route.INTERRUPT, s, frozen)
    if s >= BATCH_AT:
        return Decision(Route.BATCH, s, frozen)
    if s >= DIGEST_AT:
        return Decision(Route.DIGEST, s, frozen, "below_interrupt_bar")
    return Decision(Route.SILENT, s, frozen, "below_bar")
