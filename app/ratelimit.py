"""Lightweight in-memory token-bucket rate limiter for the programmatic API.

The app is single-node, so an in-memory limiter is sufficient and matches the
architecture (same as the run semaphore). Keyed by an opaque string — for /v1 we
key by API-key id, so a runaway or buggy client throttles itself without starving
others, and can't spawn unbounded expensive runs.

NOTE: not shared across processes. If the app is ever scaled horizontally (the
managed-cloud worker-fleet track), this must move to a shared store (e.g. Redis).
"""
import time

from . import config


class _Bucket:
    __slots__ = ("tokens", "last")

    def __init__(self, tokens: float, last: float):
        self.tokens = tokens
        self.last = last


class TokenBucketLimiter:
    """Classic token bucket. `per_min` sustained rate, `burst` bucket capacity.
    per_min <= 0 disables limiting (check() always allows)."""

    def __init__(self, per_min: int, burst: int | None = None):
        self.enabled = per_min > 0
        self.rate = per_min / 60.0                      # tokens per second
        self.capacity = float(burst if burst and burst > 0 else per_min)
        self._buckets: dict[str, _Bucket] = {}

    def check(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Consume one token for `key`. Returns (allowed, retry_after_seconds)."""
        if not self.enabled:
            return True, 0.0
        if now is None:
            now = time.monotonic()
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(self.capacity, now)
            self._buckets[key] = b
        # refill based on elapsed time, capped at capacity
        b.tokens = min(self.capacity, b.tokens + (now - b.last) * self.rate)
        b.last = now
        if b.tokens >= 1.0:
            b.tokens -= 1.0
            return True, 0.0
        retry = (1.0 - b.tokens) / self.rate if self.rate > 0 else 60.0
        return False, retry


# The /v1 limiter, keyed by API-key id.
v1_limiter = TokenBucketLimiter(config.V1_RATE_PER_MIN, config.V1_RATE_BURST)
