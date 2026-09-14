"""Delivery correctness (B3, B4, B5, B7).

These branches only execute during an outage — which is exactly when nobody is
watching and when a silent wrong answer does the most damage. They are pure
functions precisely so they can be pinned without an outage.
"""

from datetime import date, datetime

import httpx
import pytest

from app.config import IST
from app.outbox import (
    RETRY_LADDER_S,
    Drainer,
    Entry,
    OutboxState,
    SendOutcome,
    classify_error,
    idempotency_key,
    is_protective,
    next_state,
)

NOW = datetime(2026, 9, 15, 11, 0, tzinfo=IST)
MARGIN = "margin.band_up"  # protective
MARKET = "market.volume_spike"  # not


def resp(code):
    return httpx.HTTPStatusError(
        "boom",
        request=httpx.Request("POST", "http://x/send"),
        response=httpx.Response(code, request=httpx.Request("POST", "http://x/send")),
    )


# ---- idempotency ----------------------------------------------------


def test_the_key_is_scoped_to_the_day():
    """A condition that recurs tomorrow is a new alert, not a duplicate."""
    a = idempotency_key(1, MARGIN, "tight", date(2026, 9, 15))
    b = idempotency_key(1, MARGIN, "tight", date(2026, 9, 16))
    assert a != b


def test_the_key_separates_users():
    assert idempotency_key(1, MARGIN, "d", date(2026, 9, 15)) != idempotency_key(
        2, MARGIN, "d", date(2026, 9, 15)
    )


# ---- B3: retry only when nothing was spent --------------------------


def test_a_send_that_never_left_the_box_retries_on_the_ladder():
    """Nothing was executed and nothing was spent, so a re-run cannot double a
    side effect."""
    for attempt, expected in enumerate(RETRY_LADDER_S):
        state, delay = next_state(SendOutcome.NOT_ATTEMPTED, rule_id=MARKET, attempts=attempt)
        assert state is OutboxState.PENDING
        assert delay == expected


def test_the_ladder_is_bounded():
    state, delay = next_state(
        SendOutcome.NOT_ATTEMPTED, rule_id=MARKET, attempts=len(RETRY_LADDER_S)
    )
    assert state is OutboxState.FAILED and delay is None


def test_a_permanent_rejection_never_retries():
    assert next_state(SendOutcome.REJECTED, rule_id=MARGIN, attempts=0)[0] is OutboxState.FAILED


def test_delivery_is_terminal():
    assert next_state(SendOutcome.DELIVERED, rule_id=MARKET, attempts=2)[0] is OutboxState.SENT


# ---- B4: the unknown state, and our deliberate divergence -----------


def test_an_ambiguous_send_of_market_chatter_stops_at_unknown():
    """Preferring a miss to a repeat is right when the alert is information."""
    state, delay = next_state(SendOutcome.AMBIGUOUS, rule_id=MARKET, attempts=0)
    assert state is OutboxState.UNKNOWN and delay is None


def test_an_ambiguous_send_of_a_margin_alert_retries():
    """Here we diverge from the reference implementation on purpose: a
    duplicated margin warning is annoying, a dropped one costs money."""
    state, delay = next_state(SendOutcome.AMBIGUOUS, rule_id=MARGIN, attempts=0)
    assert state is OutboxState.PENDING and delay == RETRY_LADDER_S[0]


def test_even_a_protective_retry_is_bounded():
    state, _ = next_state(SendOutcome.AMBIGUOUS, rule_id=MARGIN, attempts=len(RETRY_LADDER_S))
    assert state is OutboxState.UNKNOWN


@pytest.mark.parametrize(
    "rule_id,protective",
    [
        ("margin.band_up", True),
        ("expiry.itm_short", True),
        ("greeks.delta_drift", True),
        ("market.volume_spike", False),
        ("news.position_scoped", False),
        ("pnl.day_move", False),
    ],
)
def test_protective_families(rule_id, protective):
    assert is_protective(rule_id) is protective


def test_an_unknown_rule_is_not_treated_as_protective():
    assert not is_protective("nope.unknown")


# ---- error classification -------------------------------------------


def test_a_disconnected_adapter_means_nothing_was_sent():
    assert classify_error(resp(503)) is SendOutcome.NOT_ATTEMPTED


def test_no_message_id_back_is_ambiguous_not_a_failure():
    """The adapter accepted it; we simply cannot prove it landed."""
    assert classify_error(resp(502)) is SendOutcome.AMBIGUOUS


def test_a_connection_error_never_left_this_machine():
    assert classify_error(httpx.ConnectError("refused")) is SendOutcome.NOT_ATTEMPTED


def test_a_read_timeout_may_have_landed():
    assert classify_error(httpx.ReadTimeout("slow")) is SendOutcome.AMBIGUOUS


def test_a_client_error_is_permanent():
    assert classify_error(resp(400)) is SendOutcome.REJECTED


def test_a_server_error_is_ambiguous():
    assert classify_error(resp(500)) is SendOutcome.AMBIGUOUS


# ---- B5/B7: the drainer ---------------------------------------------


class FakeStore:
    def __init__(self, entries=None, wa_id="919999999999"):
        self.entries = entries or []
        self.wa_id = wa_id
        self.finished: list[tuple] = []
        self.deferred: list[tuple] = []
        self.logged: list[dict] = []

    async def claim_outbox(self, limit=10):
        out, self.entries = self.entries[:limit], self.entries[limit:]
        return out

    async def wa_id_for(self, user_id):
        return self.wa_id

    async def finish_outbox(self, outbox_id, state, channel_msg_id=None, error=None):
        self.finished.append((outbox_id, state, channel_msg_id, error))

    async def defer_outbox(self, outbox_id, send_after):
        self.deferred.append((outbox_id, send_after))

    async def log_message(self, user_id, direction, text, channel_msg_id=None, **kw):
        self.logged.append(
            {
                "user_id": user_id,
                "direction": direction,
                "text": text,
                "channel_msg_id": channel_msg_id,
                **kw,
            }
        )
        return 1


class FakeChannel:
    def __init__(self, result="wamid.1"):
        self.result = result
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def entry(rule_id=MARKET, attempts=0, **kw):
    return Entry(
        id=1,
        user_id=7,
        rule_id=rule_id,
        fingerprint="fp",
        idempotency_key="k",
        body="hello",
        route="interrupt",
        attempts=attempts,
        **kw,
    )


async def test_a_delivered_alert_is_committed_to_the_transcript():
    """Delivery is not complete at the channel boundary. An alert is a
    conversation opener; a reply to it must land in a session that knows what
    was just said."""
    store, chan = FakeStore([entry()]), FakeChannel()
    assert await Drainer(store, chan, now=lambda: NOW).drain_once() == 1
    assert store.finished[0][1] is OutboxState.SENT
    assert store.logged and store.logged[0]["direction"] == "out"


async def test_the_transcript_row_carries_its_provenance():
    """Which rule produced it — needed for the feedback loop to attribute a
    reaction, and so a reply knows what it is replying to."""
    store, chan = FakeStore([entry(rule_id=MARGIN)]), FakeChannel()
    await Drainer(store, chan, now=lambda: NOW).drain_once()
    assert store.logged[0]["intent"] == f"alert:{MARGIN}"


async def test_an_undelivered_alert_is_not_written_to_the_transcript():
    store, chan = FakeStore([entry()]), FakeChannel(httpx.ConnectError("refused"))
    await Drainer(store, chan, now=lambda: NOW).drain_once()
    assert store.logged == []


async def test_a_transient_failure_defers_rather_than_failing():
    store, chan = FakeStore([entry()]), FakeChannel(httpx.ConnectError("refused"))
    await Drainer(store, chan, now=lambda: NOW).drain_once()
    assert store.deferred and not store.finished


async def test_a_pending_retry_produces_no_failure_notice():
    """Do not tell anyone about a failure you are about to fix silently."""
    store, chan = FakeStore([entry()]), FakeChannel(httpx.ConnectError("refused"))
    assert await Drainer(store, chan, now=lambda: NOW).drain_once() == 0
    assert store.finished == []


async def test_an_empty_message_id_is_treated_as_ambiguous_not_success():
    """A send function returning without raising is not delivery confirmation."""
    store, chan = FakeStore([entry()]), FakeChannel(result="")
    await Drainer(store, chan, now=lambda: NOW).drain_once()
    assert store.finished[0][1] is OutboxState.UNKNOWN
    assert store.logged == []


async def test_a_user_with_no_address_fails_loudly():
    store, chan = FakeStore([entry()], wa_id=None), FakeChannel()
    await Drainer(store, chan, now=lambda: NOW).drain_once()
    assert store.finished[0][1] is OutboxState.FAILED
    assert chan.sent == []


async def test_the_outbound_message_carries_the_idempotency_key():
    store, chan = FakeStore([entry()]), FakeChannel()
    await Drainer(store, chan, now=lambda: NOW).drain_once()
    assert chan.sent[0].idempotency_key == "k"
