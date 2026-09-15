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
from datetime import datetime, timedelta
from pathlib import Path as FilePath

from redis.exceptions import TimeoutError as RedisTimeoutError

from app.channel.base import Channel, InboundMessage, OutboundMessage
from app.channel.identity import canonical_wa_id
from app.config import IST, settings
from app.render.prefs import confirm as pref_reply
from app.router.fastpath import Intent, Path, classify
from app.router.prefs import QUIET_TODAY_SENTINEL, QUIET_TODAY_UNTIL, PrefAction
from app.router.prefs import parse as parse_pref
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
        crypto=None,
        deduper: Deduper | None = None,
    ) -> None:
        self._channel = channel
        self._index = index
        self._desk_for = desk_for  # (wa_id) -> Desk | None when unlinked
        self._store = store
        self._crypto = crypto
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

        # Control commands are matched before classification. "pause" through
        # the fast-path gauntlet reaches the instrument resolver and gets
        # answered with a stock, which is a memorable way to fail.
        out = await self._pref_command(text, msg.wa_id)
        route = classify(text, resolves_to_instrument=lambda q: self._index.resolve(q).ok)

        # Account-level intents need the Store, which Desk deliberately does
        # not have — it answers about markets, not about accounts.
        if out is None:
            out = await self._account_intent(route, msg.wa_id)
        if out is None:
            desk = await self._desk_for(msg.wa_id)
            out = (
                await desk.handle(route, msg.wa_id)
                if desk is not None
                else await self._onboarding(msg.wa_id)
            )

        # Reply to the address the message arrived on. One place, so no path
        # can forget and fall back to a rebuilt JID.
        out.jid = msg.jid
        channel_msg_id = await self._channel.send(out)

        # Every handled message logs, on every path. This used to sit after an
        # early return, so account intents were silently absent from the log
        # and a working link flow looked like a dropped message.
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info("intent=%s path=%s %dms", route.intent, route.path, latency_ms)
        if self._store:
            in_id = await self._store.log_message(
                None, "in", text, msg.channel_msg_id, str(route.intent), latency_ms
            )
            await self._store.log_message(None, "out", out.text, channel_msg_id)
            await self._store.log_trace(in_id, str(route.path), str(route.intent))

    async def _pref_command(self, text: str, wa_id: str) -> OutboundMessage | None:
        """Handle pause / resume / snooze / brief-time. None if not one."""
        if self._store is None:
            return None
        cmd = parse_pref(text)
        if cmd is None:
            return None

        user_id = await self._store.user_id_for(wa_id)
        until: datetime | None = None

        if cmd.action is PrefAction.PAUSE:
            await self._store.set_prefs(user_id, briefs_paused=True)
        elif cmd.action is PrefAction.RESUME:
            await self._store.set_prefs(user_id, briefs_paused=False, muted_until=None)
        elif cmd.action is PrefAction.SNOOZE:
            now = datetime.now(IST)
            if cmd.minutes == QUIET_TODAY_SENTINEL:
                until = (now + timedelta(days=1)).replace(
                    hour=QUIET_TODAY_UNTIL.hour,
                    minute=QUIET_TODAY_UNTIL.minute,
                    second=0,
                    microsecond=0,
                )
            else:
                until = now + timedelta(minutes=cmd.minutes or 0)
            await self._store.set_prefs(user_id, muted_until=until)
        elif cmd.action is PrefAction.SET_BRIEF_TIME:
            column = "brief_pre_market" if cmd.which == "pre_market" else "brief_post_close"
            await self._store.set_prefs(user_id, **{column: cmd.at})

        prefs = await self._store.get_prefs(user_id)
        log.info("pref %s for user %s", cmd.action, user_id)
        return OutboundMessage(wa_id=wa_id, kind="text", text=pref_reply(cmd, prefs, until=until))

    async def _account_intent(self, route, wa_id: str) -> OutboundMessage | None:
        """Handle meta.link / meta.unlink. Returns None for everything else."""
        if self._store is None:
            return None

        if route.intent is Intent.META_LINK:
            return await self._onboarding(wa_id, greet=False)

        if route.intent is Intent.META_UNLINK:
            user_id = await self._store.user_id_for(wa_id, create=False)
            if user_id is not None:
                await self._store.delete_user_data(user_id)
            return OutboundMessage(
                wa_id=wa_id,
                kind="text",
                text="Disconnected. Your credentials and history are deleted.",
            )
        return None

    async def _onboarding(self, wa_id: str, greet: bool = True) -> OutboundMessage:
        """A link with no token is a dead link — /link returns 410 without one."""
        url = f"{settings().public_base_url}/link"
        if self._store is not None:
            enc = self._crypto.encrypt(wa_id, aad=LINK_AAD) if self._crypto else None
            url = f"{url}?t={self._store.new_link_token(wa_id, enc)}"

        greeting = (
            "Hi — I'm your Groww desk. I can tell you about your\n"
            "holdings, positions, orders, margin and live prices.\n\n"
            if greet
            else ""
        )
        return OutboundMessage(
            wa_id=wa_id,
            kind="text",
            text=(
                f"{greeting}Connect your account: {url}\n"
                "Takes 30 seconds, expires in 10 minutes, and I'll only ever read."
            ),
        )


STREAM = "inbound"
GROUP = "workers"

# XREADGROUP holds the connection server-side for this long when the stream is
# empty, which is most of the time.
BLOCK_MS = 5000

# The client read timeout has to outlast that block. redis-py's from_url()
# default does not, so every idle poll raised TimeoutError — an idle worker
# produced a traceback every few seconds and looked broken while being fine.
SOCKET_TIMEOUT_S = BLOCK_MS / 1000 + 5

# How often to look for queued alerts. Low enough that an interrupt feels
# immediate, high enough that an idle desk is not hammering Postgres.
OUTBOX_POLL_S = 5
LINK_AAD = b"link"  # binds the encrypted identity to the link flow


async def consume(redis, worker: Worker, consumer: str = "worker-1") -> None:
    """Drain the inbound stream.

    A consumer group rather than a plain read, so a restart neither loses
    messages nor replays the ones already handled.
    """
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as exc:  # BUSYGROUP — already exists
        if "BUSYGROUP" not in str(exc):
            raise

    log.info("consuming %s as %s", STREAM, consumer)
    while True:
        try:
            batch = await redis.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=10, block=BLOCK_MS)
        except RedisTimeoutError:
            # A blocking read that found nothing. The normal idle path, not a
            # failure, so it gets no traceback and no backoff.
            continue
        except Exception:
            log.exception("stream read failed; retrying in 2s")
            await asyncio.sleep(2)
            continue

        for _stream, entries in batch or []:
            for entry_id, fields in entries:
                try:
                    await worker.handle(_to_inbound(fields))
                except Exception:
                    # Never let one bad message stall the stream.
                    log.exception("failed handling %s", entry_id)
                finally:
                    await redis.xack(STREAM, GROUP, entry_id)


def _to_inbound(fields: dict) -> InboundMessage:
    get = lambda k, d="": _decode(fields.get(k.encode(), fields.get(k, d)))  # noqa: E731
    jid = get("jid")
    # Identity is derived here and nowhere else. The adapter strips the JID to
    # digits for its own logging, which cannot distinguish a LID from a phone
    # number — so canonicalise from the full JID whenever we have one.
    return InboundMessage(
        channel_msg_id=get("channel_msg_id"),
        wa_id=canonical_wa_id(jid or get("wa_id")),
        text=get("text") or None,
        ts=int(get("ts", "0") or 0),
        quoted_id=get("quoted_id") or None,
        jid=get("jid") or None,
    )


def _decode(v) -> str:
    return v.decode() if isinstance(v, bytes) else str(v or "")


async def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import redis.asyncio as aioredis

    from app.auth.broker import TokenBroker
    from app.auth.crypto import Crypto
    from app.channel.baileys import BaileysChannel
    from app.dispatch import Desk
    from app.infra import Cache, CircuitBreaker, RateLimiter, RedisBackend
    from app.store.db import Store
    from app.tools.groww import GrowwTools
    from app.tools.instruments import ensure_csv

    cfg = settings()
    index = InstrumentIndex.from_csv(ensure_csv(FilePath("data/instruments.csv")))
    log.info("instrument master: %d instruments", len(index))

    redis = aioredis.from_url(cfg.redis_url, socket_timeout=SOCKET_TIMEOUT_S)
    backend = RedisBackend(redis)
    store = Store()
    crypto = Crypto(cfg.cred_key)
    broker = TokenBroker(store, crypto)
    channel = BaileysChannel(cfg.baileys_url)
    cache, limiter = Cache(backend), RateLimiter(backend)
    desks: dict[int, Desk] = {}

    toolsets: dict[int, GrowwTools] = {}

    async def tools_for(user_id: int) -> GrowwTools:
        if user_id not in toolsets:
            toolsets[user_id] = GrowwTools(broker, user_id, cache, limiter, CircuitBreaker())
        return toolsets[user_id]

    async def desk_for(wa_id: str) -> Desk | None:
        user_id = await store.user_id_for(wa_id)
        if not await store.is_linked(user_id):
            return None
        if user_id not in desks:
            desks[user_id] = Desk(await tools_for(user_id), index)
        return desks[user_id]

    worker = Worker(channel, index, desk_for, store=store, crypto=crypto, deduper=Deduper(redis))

    if not await channel.health():
        log.warning("adapter at %s is not connected — is it running?", cfg.baileys_url)

    # One process, three tasks. A separate watcher process is the right shape
    # once a live market feed contends for the executor that GrowwTools uses,
    # but with signals arriving over poll and a handful of users it would be
    # two deployables and a token-sharing problem bought for nothing.
    await asyncio.gather(
        consume(redis, worker),
        _drain_outbox(store, channel),
        _run_schedule(store, index, tools_for),
    )


async def _drain_outbox(store, channel) -> None:  # pragma: no cover - process loop
    """Send queued alerts. The worker owns the channel, so it owns sending."""
    from app.outbox import Drainer

    drainer = Drainer(store, channel)
    while True:
        try:
            await drainer.drain_once()
        except Exception:
            log.exception("outbox drain failed; retrying in %ss", OUTBOX_POLL_S)
        await asyncio.sleep(OUTBOX_POLL_S)


async def _run_schedule(store, index, tools_for) -> None:  # pragma: no cover - process loop
    """Briefs and the nightly instrument refresh, on the slot-ledger scheduler."""
    from app.market.calendar import assert_holidays_cover, now_ist
    from app.watcher.briefs import jobs
    from app.watcher.schedule import DailyScheduler

    # A missing holiday entry reads as "every weekday is a trading day", which
    # would send a market brief on Diwali. Refuse to schedule rather than be
    # silently wrong about whether the market is open.
    assert_holidays_cover(now_ist().year)
    await DailyScheduler(store, await jobs(store, tools_for, index)).run()


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
