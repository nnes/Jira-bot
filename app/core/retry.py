import asyncio
import functools
import logging
from typing import Callable, Tuple, Type

logger = logging.getLogger(__name__)


def with_retry(
    max_attempts: int = 3,
    backoff_base: float = 1.0,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
) -> Callable:
    """Async exponential-backoff retry decorator.

    Args:
        max_attempts: Total number of attempts (includes the first try).
        backoff_base: Initial wait in seconds; doubles on each retry.
        exceptions: Exception types that trigger a retry.
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: object, **kwargs: object) -> object:
            last_exc: Exception = RuntimeError("unreachable")
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        wait = backoff_base * (2 ** attempt)
                        logger.warning(
                            "%s attempt %d/%d failed (%s) — retrying in %.1fs",
                            func.__qualname__, attempt + 1, max_attempts, exc, wait,
                        )
                        await asyncio.sleep(wait)
            raise last_exc
        return wrapper
    return decorator
