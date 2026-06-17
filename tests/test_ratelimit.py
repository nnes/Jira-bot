"""Tests for the token-bucket rate limiter (120/min default, queue + reject)."""
import pytest

from app.core.ratelimit import RateLimiter


class _Clock:
    """Manually-advanced monotonic clock for deterministic tests."""
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.mark.asyncio
async def test_under_limit_proceeds_immediately():
    clock = _Clock()
    rl = RateLimiter(max_requests=120, window_seconds=60, time_func=clock)
    for _ in range(120):
        assert await rl.reserve(max_queue_wait_seconds=300) == 0.0


@pytest.mark.asyncio
async def test_over_limit_is_queued_with_increasing_wait():
    clock = _Clock()
    rl = RateLimiter(max_requests=120, window_seconds=60, time_func=clock)
    # Exhaust the burst
    for _ in range(120):
        assert await rl.reserve(300) == 0.0
    # Next requests must queue. Rate = 2 tokens/sec → 0.5s per slot, increasing.
    w1 = await rl.reserve(300)
    w2 = await rl.reserve(300)
    assert w1 == pytest.approx(0.5, abs=0.01)
    assert w2 == pytest.approx(1.0, abs=0.01)
    assert w2 > w1


@pytest.mark.asyncio
async def test_tokens_refill_over_time():
    clock = _Clock()
    rl = RateLimiter(max_requests=120, window_seconds=60, time_func=clock)
    for _ in range(120):
        await rl.reserve(300)
    # After 10s, 20 tokens refilled (2/s) → 20 immediate slots
    clock.advance(10)
    immediate = 0
    for _ in range(20):
        if await rl.reserve(300) == 0.0:
            immediate += 1
    assert immediate == 20
    # 21st must queue again
    assert await rl.reserve(300) > 0.0



@pytest.mark.asyncio
async def test_rejects_when_wait_exceeds_max_queue():
    clock = _Clock()
    rl = RateLimiter(max_requests=120, window_seconds=60, time_func=clock)
    for _ in range(120):
        await rl.reserve(300)
    # With a tiny max-queue-wait, the next over-limit request is rejected (-1),
    # and must NOT consume a slot (so a later generous call can still queue).
    assert await rl.reserve(max_queue_wait_seconds=0.1) == -1.0
    # A generous call still only needs ~0.5s (rejected one didn't reserve).
    assert await rl.reserve(max_queue_wait_seconds=300) == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_full_refill_caps_at_capacity():
    clock = _Clock()
    rl = RateLimiter(max_requests=120, window_seconds=60, time_func=clock)
    for _ in range(60):
        await rl.reserve(300)
    # Idle a long time — tokens cap at capacity (120), not unbounded
    clock.advance(10_000)
    count = 0
    for _ in range(120):
        if await rl.reserve(300) == 0.0:
            count += 1
    assert count == 120
