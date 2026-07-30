"""Shared retry helper. One small async decorator covers everything this
service needs - a dedicated retry framework would be overengineering
for a handful of HTTP calls."""
import asyncio
import functools
from typing import Callable, TypeVar

from utils.logger import get_logger

logger = get_logger("retry")

T = TypeVar("T")


def with_retry(attempts: int = 3, base_delay_seconds: float = 2.0):
    """Retries an async function with exponential backoff:
    base_delay, base_delay*2, base_delay*4, ...
    Re-raises the last exception if all attempts are exhausted so the
    caller (e.g. campaign_worker) can decide what to do with a
    genuinely failed lead."""

    def decorator(func: Callable[..., T]):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - intentionally broad, this is a generic retry wrapper
                    last_exc = exc
                    if attempt == attempts:
                        break
                    delay = base_delay_seconds * (2 ** (attempt - 1))
                    logger.warning(
                        "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                        func.__name__, attempt, attempts, exc, delay,
                    )
                    await asyncio.sleep(delay)
            logger.error("%s failed after %d attempts", func.__name__, attempts)
            raise last_exc

        return wrapper

    return decorator
