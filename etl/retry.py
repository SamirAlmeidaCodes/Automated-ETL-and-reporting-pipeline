"""Small exponential-backoff helper for network fetches."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Cap a single sleep so a large attempt count cannot stall the scheduler for hours.
MAX_BACKOFF_SECONDS = 30.0


class RetryableFetchError(Exception):
    """A fetch failed in a way that a later attempt might fix."""


def call_with_retry(
    func: Callable[[], T],
    *,
    attempts: int,
    backoff_seconds: float,
    retry_on: tuple[type[BaseException], ...],
    sleep: Callable[[float], None],
    operation: str,
) -> T:
    """Call `func` until it returns or the attempt budget is spent.

    `sleep` is injected so tests can record delays without waiting.
    The wait after attempt n is `backoff_seconds * 2^(n-1)`, capped at 30 seconds.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_on as exc:
            last_error = exc
            if attempt >= attempts:
                logger.error("%s failed after %s attempts: %s", operation, attempts, exc)
                break
            delay = min(MAX_BACKOFF_SECONDS, backoff_seconds * (2 ** (attempt - 1)))
            logger.warning(
                "%s failed (attempt %s/%s): %s; retrying in %.1fs",
                operation,
                attempt,
                attempts,
                exc,
                delay,
            )
            if delay > 0:
                sleep(delay)
    assert last_error is not None
    raise last_error
