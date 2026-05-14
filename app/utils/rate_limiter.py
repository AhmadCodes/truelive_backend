"""
Per-key token bucket rate limiter backed by Redis.

Used at the LMTP RCPT TO step to throttle a single `alert_address_id` to N/min
(default 60). A runaway camera exceeding the bucket is told `451 4.7.1` (Postfix
queues briefly, then defers) — ops can manually flip `is_quarantined=true` to
hard-block.

Implementation: simple fixed-window counter (Redis INCR + EXPIRE on first hit per
minute). Fixed-window has burst-edge weakness but is enough for our use case where
we just need to detect order-of-magnitude floods. Sliding-window or token-bucket
upgrades are a one-method swap if needed.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


class RateLimiter:
    """Fixed-window-per-minute limiter keyed by an arbitrary string."""

    def __init__(self, *, redis_client=None, prefix: str = "rl:alert_addr:"):
        self._client = redis_client
        self._prefix = prefix

    @property
    def client(self):
        if self._client is None:
            try:
                import redis  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("redis package required for RateLimiter") from exc
            self._client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
        return self._client

    def allow(self, key: str, *, limit: Optional[int] = None, now: Optional[int] = None) -> bool:
        """
        Returns True if the request fits inside the bucket; False if the bucket is
        full. Caller's responsibility to translate False into the right rejection
        (e.g. LMTP 451 vs HTTP 429).
        """
        max_per_min = limit if limit is not None else settings.ALERT_RATE_LIMIT_PER_MINUTE
        minute = int((now or time.time()) // 60)
        full_key = f"{self._prefix}{key}:{minute}"
        try:
            # INCR -> the new count post-increment. First hit returns 1.
            count = int(self.client.incr(full_key))
            if count == 1:
                # Expire shortly after the minute boundary so old buckets self-evict.
                self.client.expire(full_key, 65)
            return count <= max_per_min
        except Exception:  # pragma: no cover — never let the limiter block prod
            logger.exception("rate-limiter error; fail-open", extra={"key": key})
            return True


# Module-level singleton — lazily creates the Redis client on first use.
rate_limiter = RateLimiter()
