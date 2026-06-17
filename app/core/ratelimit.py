"""Async token-bucket rate limiter with a bounded wait-queue.

Default policy: 120 requests / 60s. Requests under the limit proceed immediately;
requests over the limit are *queued* (the caller awaits a reserved future slot)
rather than rejected — unless the wait would exceed ``max_queue_wait_seconds``,
in which case the request is rejected so callers don't hang indefinitely.
"""
import asyncio
import time
from typing import Callable, Optional


class RateLimiter:
    def __init__(
        self,
        max_requests: int = 120,
        window_seconds: float = 60.0,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity = float(max_requests)
        self.rate = max_requests / window_seconds  # tokens per second
        self._tokens = float(max_requests)
        self._now = time_func
        self._last = time_func()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._now()
        elapsed = max(0.0, now - self._last)
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
        self._last = now

    async def reserve(self, max_queue_wait_seconds: float) -> float:
        """Reserve a slot. Returns seconds the caller must wait before proceeding.

        - 0.0   → capacity available, proceed now.
        - > 0.0 → queued; the caller should notify the user, then await this delay.
        - -1.0  → rejected: the wait would exceed ``max_queue_wait_seconds``.
        """
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return 0.0
            # Schedule into the future by letting the token count go negative; each
            # over-limit caller reserves the next accruing slot (FIFO-ish ordering).
            deficit = 1.0 - self._tokens
            wait = deficit / self.rate
            if wait > max_queue_wait_seconds:
                return -1.0  # reject without consuming a slot
            self._tokens -= 1.0
            return wait


_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """Return the process-wide RateLimiter singleton built from settings."""
    global _limiter
    if _limiter is None:
        from app.config import settings
        _limiter = RateLimiter(
            max_requests=settings.rate_limit_max_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    return _limiter


def reset_rate_limiter() -> None:
    """Drop the singleton (tests / config reload)."""
    global _limiter
    _limiter = None
