"""Tests for app.utils.rate_limiter (Redis-backed token bucket)."""

from unittest.mock import MagicMock

import pytest

from app.utils.rate_limiter import RateLimiter


class _FakeRedis:
    """Minimal in-memory Redis replacement just for the limiter calls we make."""
    def __init__(self):
        self.store: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key: str, _seconds: int) -> bool:
        return True


def test_allows_up_to_limit_in_same_minute():
    fake = _FakeRedis()
    rl = RateLimiter(redis_client=fake)
    fixed_now = 1_700_000_000  # any constant integer
    # 5 hits all within the same minute window
    for _ in range(5):
        assert rl.allow("k1", limit=5, now=fixed_now)


def test_rejects_over_limit_in_same_minute():
    fake = _FakeRedis()
    rl = RateLimiter(redis_client=fake)
    fixed_now = 1_700_000_000
    for _ in range(5):
        assert rl.allow("k1", limit=5, now=fixed_now)
    # 6th hit in the same minute is over the limit
    assert not rl.allow("k1", limit=5, now=fixed_now)
    assert not rl.allow("k1", limit=5, now=fixed_now + 30)


def test_separate_keys_are_independent():
    fake = _FakeRedis()
    rl = RateLimiter(redis_client=fake)
    fixed_now = 1_700_000_000
    for _ in range(5):
        assert rl.allow("alpha", limit=5, now=fixed_now)
    # alpha is full, but beta is still fresh
    assert not rl.allow("alpha", limit=5, now=fixed_now)
    assert rl.allow("beta", limit=5, now=fixed_now)


def test_new_minute_resets_bucket():
    fake = _FakeRedis()
    rl = RateLimiter(redis_client=fake)
    minute1 = 1_700_000_000
    minute2 = minute1 + 60
    for _ in range(5):
        assert rl.allow("k", limit=5, now=minute1)
    assert not rl.allow("k", limit=5, now=minute1)
    # Next minute starts a fresh bucket
    assert rl.allow("k", limit=5, now=minute2)


def test_fails_open_on_redis_error():
    """If Redis errors out, the limiter must not block production traffic."""
    broken = MagicMock()
    broken.incr.side_effect = RuntimeError("connection refused")
    rl = RateLimiter(redis_client=broken)
    assert rl.allow("k", limit=1, now=1_700_000_000)
