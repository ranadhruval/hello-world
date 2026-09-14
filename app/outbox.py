"""The outbox: never send from inside rule evaluation (spec §18).

An alert the system believes it delivered but did not is worse than no alert.
Rules write rows here; a drainer in the worker process sends them, because the
adapter owns a single WhatsApp session and one process should serialise it.

Three defences, and a fourth we add for this domain:

1. **Outbox** — the watcher can die mid-send without losing the alert.
2. **Receiver-side verification** — `sent` requires a `channel_msg_id` back
   from the channel. A send function returning without raising is not delivery.
3. **Supersession** — a newer trigger for the same fingerprint marks the older
   pending row superseded rather than sending both. This is the bug that makes
   an assistant look broken after a network blip.
4. **An `unknown` outcome, with a per-family retry rule.** Hermes marks an
   ambiguous delivery unknown and never retries it, reasoning that losing a
   delivery is safer than duplicating a possibly-completed one. That is right
   for chat and wrong here: a duplicated margin warning is mildly annoying, a
   dropped one costs money. So protective families retry an ambiguous send and
   everything else does not.

Delivery is not complete until the message is also committed to the
conversation transcript. An alert is a conversation opener — a free-text reply
to it routes into the agent "with the alert as context" (spec §17.3), which is
impossible if the alert was never in the history.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum

from app.channel.base import OutboundMessage
from app.config import IST
from app.watcher.rules import REGISTRY, Family

log = logging.getLogger(__name__)


class OutboxState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPERSEDED = "superseded"
    SHADOW = "shadow"  # composed and scored, deliberately not sent
    UNKNOWN = "unknown"  # may or may not have reached the user


class SendOutcome(StrEnum):
    """What we can actually know after asking the channel to send."""

    DELIVERED = "delivered"  # channel returned an id
    NOT_ATTEMPTED = "not_attempted"  # transient; nothing left this process
    AMBIGUOUS = "ambiguous"  # accepted, but no id came back
    REJECTED = "rejected"  # permanent


# Families where a missed alert costs money. These retry an ambiguous send and
# accept the risk of a duplicate; everything else prefers silence to a repeat.
PROTECTIVE = frozenset({Family.MARGIN, Family.EXPIRY, Family.STRUCTURE})

# Hermes' ladder. Re-runs only happen when nothing was spent, so a retry cannot
# double a side effect.
RETRY_LADDER_S = (300, 900, 1800)


def idempotency_key(user_id: int, rule_id: str, dedupe: str, day: date) -> str:
    """Keyed on the day so a condition that recurs tomorrow is a new alert."""
    return f"{user_id}:{rule_id}:{dedupe}:{day:%Y-%m-%d}"


def is_protective(rule_id: str) -> bool:
    rule = REGISTRY.get(rule_id)
    return rule is not None and rule.family in PROTECTIVE


def next_state(
    outcome: SendOutcome, *, rule_id: str, attempts: int
) -> tuple[OutboxState, int | None]:
    """The state machine. Returns the new state and seconds until a retry.

    Pure, so the policy is testable without a database or a channel — which
    matters because these branches are the ones that only fire during an
    outage, i.e. exactly when nobody is watching.
    """
    if outcome is SendOutcome.DELIVERED:
        return OutboxState.SENT, None

    if outcome is SendOutcome.REJECTED:
        return OutboxState.FAILED, None

    if outcome is SendOutcome.NOT_ATTEMPTED:
        # Nothing was executed and nothing was spent, so re-running cannot
        # double a side effect. Bounded: three tries, then give up loudly.
        if attempts < len(RETRY_LADDER_S):
            return OutboxState.PENDING, RETRY_LADDER_S[attempts]
        return OutboxState.FAILED, None

    # AMBIGUOUS — the send may have landed. Only protective families try again.
    if is_protective(rule_id) and attempts < len(RETRY_LADDER_S):
        return OutboxState.PENDING, RETRY_LADDER_S[attempts]
    return OutboxState.UNKNOWN, None


def classify_error(exc: BaseException) -> SendOutcome:
    """Map a send failure onto what we can honestly claim about it."""
    import httpx

    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 503:
            return SendOutcome.NOT_ATTEMPTED  # adapter says it is not connected
        if code == 502:
            return SendOutcome.AMBIGUOUS  # accepted but returned no id
        if 400 <= code < 500:
            return SendOutcome.REJECTED
        return SendOutcome.AMBIGUOUS  # 5xx: the send may have gone out
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return SendOutcome.NOT_ATTEMPTED  # never left this box
    if isinstance(exc, httpx.HTTPError):
        return SendOutcome.AMBIGUOUS  # read timeout: may have landed
    return SendOutcome.REJECTED


@dataclass
class Entry:
    """One queued alert."""

    id: int
    user_id: int
    rule_id: str
    fingerprint: str
    idempotency_key: str
    body: str
    route: str
    payload: dict = field(default_factory=dict)
    attempts: int = 0
    jid: str | None = None
    signal_id: str = ""

    def to_outbound(self, wa_id: str) -> OutboundMessage:
        return OutboundMessage(
            wa_id=wa_id,
            kind="text",
            text=self.body,
            jid=self.jid,
            idempotency_key=self.idempotency_key,
        )


class Drainer:
    """Claims queued alerts and sends them, in the worker process.

    The worker rather than the watcher because the adapter holds one WhatsApp
    session: one process owning the channel serialises sends and typing
    indicators, and a watcher crash mid-send leaves the row intact.
    """

    def __init__(self, store, channel, now=None) -> None:
        self._store = store
        self._channel = channel
        self._now = now or (lambda: datetime.now(IST))

    async def drain_once(self, limit: int = 10) -> int:
        sent = 0
        for entry in await self._store.claim_outbox(limit=limit):
            if await self._deliver(entry):
                sent += 1
        return sent

    async def _deliver(self, entry: Entry) -> bool:
        wa_id = await self._store.wa_id_for(entry.user_id)
        if not wa_id:
            log.error("outbox %s has no address for user %s", entry.id, entry.user_id)
            await self._store.finish_outbox(entry.id, OutboxState.FAILED, error="no address")
            return False

        try:
            channel_msg_id = await self._channel.send(entry.to_outbound(wa_id))
            outcome = SendOutcome.DELIVERED if channel_msg_id else SendOutcome.AMBIGUOUS
        except Exception as exc:
            channel_msg_id, outcome = None, classify_error(exc)
            log.warning("outbox %s send failed (%s): %s", entry.id, outcome, exc)

        state, retry_in = next_state(outcome, rule_id=entry.rule_id, attempts=entry.attempts)

        if state is OutboxState.PENDING:
            await self._store.defer_outbox(
                entry.id, send_after=self._now() + timedelta(seconds=retry_in or 0)
            )
            # Hermes: while a retry is pending the failure notice is suppressed.
            # Do not tell anyone about a failure you are about to fix silently.
            return False

        await self._store.finish_outbox(
            entry.id,
            state,
            channel_msg_id=channel_msg_id,
            error=None if state is OutboxState.SENT else str(outcome),
        )

        if state is OutboxState.SENT:
            # Delivery is complete only once the message is also in the
            # transcript. Carries its provenance so a reply lands in a
            # conversation that knows what was just said, and so the feedback
            # loop can attribute a reaction to the rule that caused it.
            await self._store.log_message(
                entry.user_id,
                "out",
                entry.body,
                channel_msg_id,
                intent=f"alert:{entry.rule_id}",
            )
        elif state is OutboxState.UNKNOWN:
            log.error(
                "outbox %s (%s) is UNKNOWN — may or may not have been delivered; "
                "not retrying a non-protective family",
                entry.id,
                entry.rule_id,
            )
        return state is OutboxState.SENT
