"""
Async retry utility with exponential backoff and rate limit awareness.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger("otto.utils.retry")

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 10.0,
    retryable_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
) -> T:
    """
    Execute an async callable with exponential backoff and jitter.

    If an exception occurs or an HTTP response has a retryable status code,
    retries up to max_retries times. Respects Retry-After header when available.
    """
    attempt = 0
    while True:
        try:
            result = await fn()
            # If the result is an httpx Response, check status code
            if hasattr(result, "status_code") and result.status_code in retryable_statuses:
                if attempt >= max_retries:
                    return result
                
                # Check for Retry-After header
                retry_after = None
                if hasattr(result, "headers") and "retry-after" in result.headers:
                    try:
                        retry_after = float(result.headers["retry-after"])
                    except (ValueError, TypeError):
                        pass

                delay = retry_after if retry_after is not None else min(
                    max_delay,
                    base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                )
                logger.warning(
                    "HTTP %d encountered, retrying in %.2fs (attempt %d/%d)",
                    result.status_code, delay, attempt + 1, max_retries
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            return result

        except Exception as e:
            if attempt >= max_retries:
                raise
            delay = min(max_delay, base_delay * (2 ** attempt) + random.uniform(0, 0.5))
            logger.warning(
                "Operation failed (%s), retrying in %.2fs (attempt %d/%d)",
                e, delay, attempt + 1, max_retries
            )
            await asyncio.sleep(delay)
            attempt += 1
