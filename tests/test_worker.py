import asyncio

from app.channel.base import InboundMessage, OutboundMessage
from app.channel.console import ConsoleChannel, inbound
from app.worker import Debouncer, Deduper


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
