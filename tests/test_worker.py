import asyncio
from unittest import mock

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.channel.base import InboundMessage, OutboundMessage
from app.channel.console import ConsoleChannel, inbound
from app.tools.instruments import InstrumentIndex
from app.worker import (
    BLOCK_MS,
    SOCKET_TIMEOUT_S,
    Debouncer,
    Deduper,
    Worker,
    _to_inbound,
    consume,
)


class FakeStore:
    """Just the Store surface the Worker touches."""

    def __init__(self, linked: bool = True) -> None:
        self.linked = linked
        self.deleted: list[int] = []
        self.tokens_issued = 0
        self.logged: list[tuple] = []

    async def user_id_for(self, wa_id, create=True):
        return 1

    async def is_linked(self, user_id):
        return self.linked

    async def delete_user_data(self, user_id):
        self.deleted.append(user_id)

    def new_link_token(self, wa_id):
        self.tokens_issued += 1
        return f"tok{self.tokens_issued}"

    async def log_message(self, *a, **kw):
        self.logged.append(a)
        return 1

    async def log_trace(self, *a, **kw):
        return None


def make_worker(store=None, desk=None, index=None):
    channel = ConsoleChannel()

    async def desk_for(wa_id):
        return desk

    idx = index if index is not None else InstrumentIndex([])
    return Worker(channel, idx, desk_for=desk_for, store=store), channel


async def test_duplicate_message_is_dropped():
    """WhatsApp redelivers, so the same id must only be processed once."""
    d = Deduper()
    assert not await d.is_duplicate("abc")
    assert await d.is_duplicate("abc")


async def test_distinct_ids_are_independent():
    d = Deduper()
    assert not await d.is_duplicate("a")
    assert not await d.is_duplicate("b")


async def test_debouncer_concatenates_fragments():
    """'nifty' then '25000 ce' in quick succession is one question."""
    got: list[str] = []

    async def ready(msg, text):
        got.append(text)

    d = Debouncer(window_ms=30, on_ready=ready)
    await d.feed(inbound("nifty", msg_id="1"))
    await d.feed(inbound("25000 ce", msg_id="2"))
    await asyncio.sleep(0.1)

    assert got == ["nifty\n25000 ce"]


async def test_debouncer_keeps_chats_separate():
    got: list[tuple[str, str]] = []

    async def ready(msg, text):
        got.append((msg.wa_id, text))

    d = Debouncer(window_ms=30, on_ready=ready)
    await d.feed(inbound("portfolio", wa_id="111", msg_id="1"))
    await d.feed(inbound("margin", wa_id="222", msg_id="2"))
    await asyncio.sleep(0.1)

    assert sorted(got) == [("111", "portfolio"), ("222", "margin")]


async def test_exact_fast_path_skips_the_debounce_window():
    """'portfolio' must answer instantly, not after 1.5s."""
    got: list[str] = []

    async def ready(msg, text):
        got.append(text)

    d = Debouncer(window_ms=5000, on_ready=ready)
    await d.feed(inbound("portfolio"), immediate=True)

    assert got == ["portfolio"]  # no sleep needed


async def test_blank_message_is_ignored():
    got: list[str] = []

    async def ready(msg, text):
        got.append(text)

    d = Debouncer(window_ms=10, on_ready=ready)
    await d.feed(inbound("   "))
    await asyncio.sleep(0.05)
    assert got == []


async def test_console_channel_returns_a_message_id():
    """A send is only delivered once the channel hands back an id (spec §18.2)."""
    ch = ConsoleChannel()
    msg_id = await ch.send(OutboundMessage(wa_id="1", kind="text", text="hi"))
    assert msg_id
    assert ch.sent[0].text == "hi"


def test_outbound_rejects_too_many_buttons():
    import pytest

    with pytest.raises(ValueError):
        OutboundMessage(wa_id="1", kind="buttons", text="x", buttons=["a", "b", "c", "d"])


def test_outbound_rejects_oversize_text():
    import pytest

    with pytest.raises(ValueError):
        OutboundMessage(wa_id="1", kind="text", text="x" * 5000)


def test_inbound_helper_builds_a_valid_message():
    msg = inbound("portfolio")
    assert isinstance(msg, InboundMessage)
    assert msg.text == "portfolio" and msg.wa_id and msg.channel_msg_id


# ---- link flow -----------------------------------------------------


async def test_link_reply_carries_a_token():
    """A bare /link URL is a dead link — the page returns 410 without one."""
    store = FakeStore()
    worker, channel = make_worker(store=store)
    await worker.handle(inbound("link"))

    text = channel.sent[0].text
    assert "?t=tok1" in text
    assert store.tokens_issued == 1


async def test_unlinked_user_gets_a_tokenised_onboarding_link():
    store = FakeStore(linked=False)
    worker, channel = make_worker(store=store)
    await worker.handle(inbound("portfolio"))

    text = channel.sent[0].text
    assert "?t=tok1" in text
    assert "only ever read" in text


async def test_each_link_request_issues_a_fresh_token():
    """Tokens are single-use, so a second ask must not reuse the first."""
    store = FakeStore()
    worker, channel = make_worker(store=store)
    await worker.handle(inbound("link", msg_id="a"))
    await worker.handle(inbound("link", msg_id="b"))

    assert store.tokens_issued == 2
    assert channel.sent[0].text != channel.sent[1].text


async def test_unlink_deletes_user_data():
    store = FakeStore()
    worker, channel = make_worker(store=store)
    await worker.handle(inbound("unlink"))

    assert store.deleted == [1]
    assert "deleted" in channel.sent[0].text.lower()


async def test_account_intents_never_reach_the_desk():
    """Desk has no Store and answers about markets, not accounts."""
    store = FakeStore()

    class ExplodingDesk:
        async def handle(self, route, wa_id):
            raise AssertionError("account intent must not reach Desk")

    worker, channel = make_worker(store=store, desk=ExplodingDesk())
    await worker.handle(inbound("link"))
    assert len(channel.sent) == 1


async def test_worker_without_a_store_still_answers():
    """The console REPL runs with no Store at all."""
    store = None

    class Desk:
        async def handle(self, route, wa_id):
            return OutboundMessage(wa_id=wa_id, kind="text", text="ok")

    worker, channel = make_worker(store=store, desk=Desk())
    await worker.handle(inbound("portfolio"))
    assert channel.sent[0].text == "ok"


# ---- redis stream decoding -----------------------------------------


def test_to_inbound_decodes_byte_fields():
    """redis-py returns bytes; the adapter writes strings."""
    msg = _to_inbound({
        b"channel_msg_id": b"ABC123",
        b"wa_id": b"919999999999",
        b"text": b"portfolio",
        b"ts": b"1757700000000",
        b"quoted_id": b"",
    })
    assert msg.channel_msg_id == "ABC123"
    assert msg.wa_id == "919999999999"
    assert msg.text == "portfolio"
    assert msg.ts == 1757700000000
    assert msg.quoted_id is None


def test_to_inbound_handles_str_fields():
    msg = _to_inbound({"channel_msg_id": "X", "wa_id": "91", "text": "margin", "ts": "0"})
    assert (msg.channel_msg_id, msg.wa_id, msg.text, msg.ts) == ("X", "91", "margin", 0)


def test_to_inbound_survives_a_missing_timestamp():
    assert _to_inbound({"wa_id": "91", "text": "hi"}).ts == 0


# ---- blocking-read timeouts ----------------------------------------


def test_socket_timeout_outlasts_the_block_window():
    """XREADGROUP holds the connection for BLOCK_MS. If the client read
    timeout is not longer, every idle poll raises and the worker looks broken
    while being completely healthy."""
    assert SOCKET_TIMEOUT_S > BLOCK_MS / 1000


async def test_idle_timeout_is_not_treated_as_a_failure():
    """A blocking read that finds nothing is the normal path — it must not
    log a traceback or back off."""
    calls = {"reads": 0, "sleeps": 0}

    class TimingOutRedis:
        async def xgroup_create(self, *a, **kw):
            return True

        async def xreadgroup(self, *a, **kw):
            calls["reads"] += 1
            if calls["reads"] <= 3:
                raise RedisTimeoutError("Timeout reading from localhost:6379")
            raise asyncio.CancelledError

        async def xack(self, *a, **kw):
            return 1

    async def counting_sleep(_):
        calls["sleeps"] += 1

    with mock.patch("app.worker.asyncio.sleep", counting_sleep):
        with pytest.raises(asyncio.CancelledError):
            await consume(TimingOutRedis(), object(), "test")

    assert calls["reads"] == 4
    assert calls["sleeps"] == 0, "an idle timeout must not trigger the retry backoff"


async def test_a_real_read_error_still_backs_off():
    calls = {"reads": 0, "sleeps": 0}

    class BrokenRedis:
        async def xgroup_create(self, *a, **kw):
            return True

        async def xreadgroup(self, *a, **kw):
            calls["reads"] += 1
            if calls["reads"] <= 2:
                raise ConnectionError("connection refused")
            raise asyncio.CancelledError

        async def xack(self, *a, **kw):
            return 1

    async def counting_sleep(_):
        calls["sleeps"] += 1

    with mock.patch("app.worker.asyncio.sleep", counting_sleep):
        with pytest.raises(asyncio.CancelledError):
            await consume(BrokenRedis(), object(), "test")

    assert calls["sleeps"] == 2
