import pytest

from app.infra import (
    Cache,
    CircuitBreaker,
    CircuitOpen,
    MemoryBackend,
    RateLimiter,
    RateLimitExceeded,
)


async def test_cache_roundtrip():
    c = Cache(MemoryBackend())
    await c.set("k", {"a": 1}, ttl=10)
    assert await c.get("k") == {"a": 1}


async def test_cache_miss_is_none():
    assert await Cache(MemoryBackend()).get("nope") is None


async def test_zero_ttl_does_not_store():
    """Order-margin quotes must never be served from cache."""
    c = Cache(MemoryBackend())
    await c.set("k", 1, ttl=0)
    assert await c.get("k") is None


async def test_rate_limiter_allows_within_budget():
    rl = RateLimiter(MemoryBackend(), limits={"live_data": (10, 300)})
    for _ in range(10):
        await rl.check("live_data")


async def test_rate_limiter_trips_on_per_second_budget():
    rl = RateLimiter(MemoryBackend(), limits={"live_data": (3, 300)})
    for _ in range(3):
        await rl.check("live_data")
    with pytest.raises(RateLimitExceeded):
        await rl.check("live_data")


async def test_rate_limiter_groups_are_independent():
    """Limits are per type-group, shared across every API in the group."""
    rl = RateLimiter(MemoryBackend(), limits={"live_data": (1, 10), "non_trading": (1, 10)})
    await rl.check("live_data")
    await rl.check("non_trading")
    with pytest.raises(RateLimitExceeded):
        await rl.check("live_data")


def test_breaker_opens_after_threshold():
    cb = CircuitBreaker(threshold=3)
    for _ in range(3):
        cb.record_failure()
    assert cb.is_open
    with pytest.raises(CircuitOpen):
        cb.guard()


def test_breaker_stays_closed_below_threshold():
    cb = CircuitBreaker(threshold=5)
    for _ in range(4):
        cb.record_failure()
    assert not cb.is_open
    cb.guard()


def test_success_resets_the_breaker():
    cb = CircuitBreaker(threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    assert not cb.is_open


def test_breaker_reopens_after_cooldown():
    cb = CircuitBreaker(threshold=2, open_s=0.0)
    cb.record_failure()
    cb.record_failure()
    assert not cb.is_open  # cooldown of 0 means it has already lapsed
