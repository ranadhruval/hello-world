"""Cross-process primitives: cache, per-type-group rate limiter, circuit breaker.

All three are Redis-backed so the api, worker and watcher processes share one
budget. Each falls back to an in-process implementation when Redis is absent,
which is what the tests and the console REPL use.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import RATE_LIMITS


class Backend(Protocol):
    async def get(self, key: str) -> str | None: ...
    async def setex(self, key: str, ttl: int, value: str) -> None: ...
    async def incr_window(self, key: str, window_s: int) -> int: ...


class MemoryBackend:
    def __init__(self) -> None:
        self._v: dict[str, tuple[float, str]] = {}
        self._counters: dict[str, list[float]] = {}

    async def get(self, key: str) -> str | None:
        hit = self._v.get(key)
        if not hit:
            return None
        expires, value = hit
        if time.monotonic() >= expires:
            del self._v[key]
            return None
        return value

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._v[key] = (time.monotonic() + ttl, value)

    async def incr_window(self, key: str, window_s: int) -> int:
        now = time.monotonic()
        hits = [t for t in self._counters.get(key, []) if now - t < window_s]
        hits.append(now)
        self._counters[key] = hits
        return len(hits)


class RedisBackend:
    def __init__(self, redis) -> None:
        self._r = redis

    async def get(self, key: str) -> str | None:
        v = await self._r.get(key)
        return v.decode() if isinstance(v, bytes) else v

    async def setex(self, key: str, ttl: int, value: str) -> None:
        await self._r.setex(key, ttl, value)

    async def incr_window(self, key: str, window_s: int) -> int:
        now = time.time()
        pipe = self._r.pipeline()
        pipe.zremrangebyscore(key, 0, now - window_s)
        pipe.zadd(key, {f"{now}:{id(self)}": now})
        pipe.zcard(key)
        pipe.expire(key, window_s + 1)
        return (await pipe.execute())[2]


class Cache:
    def __init__(self, backend: Backend | None = None) -> None:
        self._b = backend or MemoryBackend()

    async def get(self, key: str) -> Any | None:
        raw = await self._b.get(key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any, ttl: int) -> None:
        if ttl > 0:
            await self._b.setex(key, ttl, json.dumps(value, default=str))


class RateLimitExceeded(RuntimeError):
    """The type-group budget is dry. Serve cache with a staleness label."""

    def __init__(self, group: str, per: str) -> None:
        super().__init__(f"{group} rate limit exhausted ({per})")
        self.group = group


class RateLimiter:
    """Token bucket per Groww type-group (spec §5.5).

    Limits are shared across every API in a group, so this counts by group,
    not by endpoint. At ~30 users the binding constraint is Live Data at
    300/min.
    """

    def __init__(self, backend: Backend | None = None, limits=None) -> None:
        self._b = backend or MemoryBackend()
        self._limits = limits or RATE_LIMITS

    async def check(self, group: str) -> None:
        per_sec, per_min = self._limits[group]
        if await self._b.incr_window(f"rl:{group}:s", 1) > per_sec:
            raise RateLimitExceeded(group, f"{per_sec}/s")
        if await self._b.incr_window(f"rl:{group}:m", 60) > per_min:
            raise RateLimitExceeded(group, f"{per_min}/min")


class CircuitOpen(RuntimeError):
    pass


@dataclass
class CircuitBreaker:
    """5 failures in 60s opens for 120s (spec §4.6)."""

    threshold: int = 5
    window_s: float = 60.0
    open_s: float = 120.0
    _failures: list[float] = None  # type: ignore[assignment]
    _opened_at: float | None = None

    def __post_init__(self) -> None:
        self._failures = []

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at >= self.open_s:
            self._opened_at = None
            self._failures.clear()
            return False
        return True

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None

    def record_failure(self) -> None:
        now = time.monotonic()
        self._failures = [t for t in self._failures if now - t < self.window_s]
        self._failures.append(now)
        if len(self._failures) >= self.threshold:
            self._opened_at = now

    def guard(self) -> None:
        if self.is_open:
            raise CircuitOpen("Groww API circuit is open — serving cache")
