"""Watches the desk proposes, and the user accepts (B10).

A suggestion is a ready-to-create watch the user accepts with one reply or
dismisses forever. It turns a passive observation — you have asked about TITAN
four times this week — into a standing instruction they consented to, which is
the difference between an assistant that notices and one that presumes.

Three rules carried over wholesale, each guarding a real failure:

**Nothing auto-creates.** A proposal is a proposal. An assistant that starts
watching things on your behalf is one you have to audit, and nobody audits.

**A cap, so the list is never a nag wall.** Five pending at most. Beyond that
new proposals are dropped rather than queued: a backlog of suggestions is not a
feature, it is the thing that makes someone mute the whole channel.

**A dismissal is permanent, latched by key.** Re-offering something already
refused is the single fastest way to train someone to ignore you — and they
will ignore the good proposals with it.

Accepting inserts an ordinary `watches` row. One watch engine, not two: a
suggestion that created its own parallel construct would drift from the real
thing within a month.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

MAX_PENDING = 5

# How many times a name must come up unprompted before proposing a watch.
# Below three it is a passing curiosity, and proposing on one mention makes the
# desk feel like it is reading over your shoulder.
REPEAT_THRESHOLD = 3


class Source(StrEnum):
    ASKED_REPEATEDLY = "asked_repeatedly"
    POSITION_OPENED = "position_opened"
    CATALOG = "catalog"


class State(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"


@dataclass(frozen=True)
class Suggestion:
    dedup_key: str
    source: Source
    reason: str  # shown verbatim; the user's terms, not ours
    spec: dict = field(default_factory=dict)
    state: State = State.PENDING


def dedup_key(source: Source, symbol: str) -> str:
    return f"{source}|{symbol.upper()}"


def may_propose(candidate: Suggestion, existing: list[Suggestion]) -> tuple[bool, str]:
    """Whether to offer this. Returns the decision and the reason for it.

    Pure, because these three rules are the whole product surface of the
    feature and each is one line — which is exactly the kind of code that gets
    quietly broken by a later refactor unless it is pinned.
    """
    for s in existing:
        if s.dedup_key == candidate.dedup_key:
            if s.state is State.DISMISSED:
                return False, "dismissed_before"
            return False, "already_proposed"
    if sum(1 for s in existing if s.state is State.PENDING) >= MAX_PENDING:
        return False, "pending_full"
    return True, ""


def from_repeated_questions(symbol: str, asks: int) -> Suggestion | None:
    """You keep asking about this — want me to watch it?"""
    if asks < REPEAT_THRESHOLD:
        return None
    return Suggestion(
        dedup_key=dedup_key(Source.ASKED_REPEATEDLY, symbol),
        source=Source.ASKED_REPEATEDLY,
        reason=f"You've asked about {symbol.upper()} {asks} times this week.",
        spec={"trading_symbol": symbol.upper(), "condition": "pct_move", "value": 3.0},
    )


def from_new_position(symbol: str, segment: str) -> Suggestion:
    """A position opened with nothing watching it."""
    return Suggestion(
        dedup_key=dedup_key(Source.POSITION_OPENED, symbol),
        source=Source.POSITION_OPENED,
        reason=f"You opened a position in {symbol.upper()} and aren't watching it.",
        spec={
            "trading_symbol": symbol.upper(),
            "segment": segment,
            "condition": "pct_move",
            "value": 3.0,
        },
    )


def render(pending: list[Suggestion]) -> str:
    """The offer, as a numbered reply — Baileys does not render buttons."""
    if not pending:
        return ""
    lines = ["Want me to watch these?", ""]
    lines += [f"  {i}  {s.reason}" for i, s in enumerate(pending, 1)]
    lines += ["", 'Reply with a number to start, or "no" to drop them.']
    return "\n".join(lines)


class Suggestions:
    """Typed accessor. Accepting goes through the ordinary watch path."""

    def __init__(self, store) -> None:
        self._store = store

    async def offer(self, user_id: int, candidate: Suggestion) -> bool:
        existing = await self._store.suggestions_for(user_id)
        ok, _reason = may_propose(candidate, existing)
        if ok:
            await self._store.add_suggestion(user_id, candidate)
        return ok

    async def pending(self, user_id: int) -> list[Suggestion]:
        return [s for s in await self._store.suggestions_for(user_id) if s.state is State.PENDING]

    async def accept(self, user_id: int, dedup_key_: str) -> bool:
        """Creates a real watch row — never a parallel construct."""
        suggestion = await self._store.get_suggestion(user_id, dedup_key_)
        if suggestion is None or suggestion.state is not State.PENDING:
            return False
        await self._store.add_watch(user_id, **suggestion.spec)
        await self._store.decide_suggestion(user_id, dedup_key_, State.ACCEPTED)
        return True

    async def dismiss(self, user_id: int, dedup_key_: str) -> None:
        await self._store.decide_suggestion(user_id, dedup_key_, State.DISMISSED)
