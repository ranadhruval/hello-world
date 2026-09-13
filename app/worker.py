"""Inbound consumer (spec §2.2).

Lifecycle per message:
  1. dedupe on channel_msg_id — WhatsApp redelivers
  2. debounce 1.5s and concatenate — users send thoughts in fragments
  3. resolve user, classify, dispatch
  4. send typing if the answer is taking longer than 1.2s
  5. write a trace row
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.channel.base import Channel, InboundMessage
from app.config import settings
from app.render import templates as tpl
from app.router.fastpath import Path, classify
from app.tools.instruments import InstrumentIndex

log = logging.getLogger(__name__)


class Deduper:
    """WhatsApp redelivers, so every message id is seen at most once.

    Redis SETNX with a 24h TTL in production; this in-process fallback keeps
    the console REPL and the tests honest.
    """

    def __init__(self, redis=None, ttl_s: int = 86_400) -> None:
        self._redis = redis
        self._ttl = ttl_s
        self._seen: dict[str, float] = {}

    async def is_duplicate(self, message_id: str) -> bool:
        if self._redis is not None:
            return not await self._redis.set(f"msg:{message_id}", 1, ex=self._ttl, nx=True)

        now = time.monotonic()
        self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}
        if message_id in self._seen:
            return True
        self._seen[message_id] = now
        return False


@dataclass
class _Pending:
    parts: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    first_id: str = ""


class Debouncer:
    """Buffer a chat for `window_ms` after its last message, then concatenate.

    Users send "nifty", then "25000 ce", then "what's the OI" as three
    messages in four seconds and mean one question. A message that already
    matches a fast-path intent exactly skips the wait, so "portfolio" still
    answers instantly.
    """

    def __init__(self, window_ms: int, on_ready) -> None:
        self._window = window_ms / 1000
        self._on_ready = on_ready
        self._pending: dict[str, _Pending] = {}

    async def feed(self, msg: InboundMessage, *, immediate: bool = False) -> None:
        text = (msg.text or "").strip()
        if not text:
            return

        if immediate and msg.wa_id not in self._pending:
            await self._on_ready(msg, text)
            return

        slot = self._pending.setdefault(msg.wa_id, _Pending(first_id=msg.channel_msg_id))
        slot.parts.append(text)
        if slot.task:
            slot.task.cancel()
        slot.task = asyncio.create_task(self._flush_after(msg))

    async def _flush_after(self, msg: InboundMessage) -> None:
        try:
            await asyncio.sleep(self._window)
        except asyncio.CancelledError:
            return
        slot = self._pending.pop(msg.wa_id, None)
        if slot and slot.parts:
            await self._on_ready(msg, "\n".join(slot.parts))

    async def drain(self) -> None:
        """Flush everything now — used at shutdown and in tests."""
        for wa_id in list(self._pending):
            slot = self._pending.pop(wa_id)
            if slot.task:
                slot.task.cancel()


class Worker:
    def __init__(
        self,
        channel: Channel,
        index: InstrumentIndex,
        desk_for,
        store=None,
        deduper: Deduper | None = None,
    ) -> None:
        self._channel = channel
        self._index = index
        self._desk_for = desk_for  # (wa_id) -> Desk | None when unlinked
        self._store = store
        self._dedupe = deduper or Deduper()
        self._debounce = Debouncer(settings().debounce_ms, self._process)

    async def handle(self, msg: InboundMessage) -> None:
        if await self._dedupe.is_duplicate(msg.channel_msg_id):
            log.debug("dropping duplicate %s", msg.channel_msg_id)
            return
        route = classify(msg.text or "")
        # An exact fast-path hit does not wait out the debounce window.
        await self._debounce.feed(msg, immediate=route.path is Path.FAST)

    async def _process(self, msg: InboundMessage, text: str) -> None:
        typing = asyncio.create_task(self._typing_after(msg.wa_id))
        try:
            await self._respond(msg, text)
        finally:
            typing.cancel()
            await self._channel.typing(msg.wa_id, False)

    async def _typing_after(self, wa_id: str) -> None:
        """Only show typing once the answer is visibly slow (spec §3.4)."""
        try:
            await asyncio.sleep(settings().typing_after_ms / 1000)
            await self._channel.typing(wa_id, True)
        except asyncio.CancelledError:
            pass

    async def _respond(self, msg: InboundMessage, text: str) -> None:
        started = time.monotonic()
        desk = await self._desk_for(msg.wa_id)
        if desk is None:
            await self._channel.send(_onboarding(msg.wa_id))
            return

        route = classify(
            text, resolves_to_instrument=lambda q: self._index.resolve(q).ok
        )
        out = await desk.handle(route, msg.wa_id)
        channel_msg_id = await self._channel.send(out)

        latency_ms = int((time.monotonic() - started) * 1000)
        log.info("intent=%s path=%s %dms", route.intent, route.path, latency_ms)
        if self._store:
            in_id = await self._store.log_message(
                None, "in", text, msg.channel_msg_id, str(route.intent), latency_ms
            )
            await self._store.log_message(None, "out", out.text, channel_msg_id)
            await self._store.log_trace(in_id, str(route.path), str(route.intent))


def _onboarding(wa_id: str):
    from app.channel.base import OutboundMessage

    return OutboundMessage(
        wa_id=wa_id,
        kind="buttons",
        text=(
            "Hi — I'm your Groww desk. I can tell you about your\n"
            "holdings, positions, orders, margin and live prices.\n\n"
            f"First, connect your account: {settings().public_base_url}/link\n"
            "Takes 30 seconds, and I'll only ever read."
        ),
        buttons=["Connect Groww"],
    )


async def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("worker starting; %s", tpl.HELP.splitlines()[0])
    # Wiring the Redis stream consumer is the next step; app/repl.py drives
    # the same Worker over stdin today.
    await asyncio.Event().wait()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
