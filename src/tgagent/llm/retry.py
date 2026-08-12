"""Retry with exponential backoff and full jitter.

Used by every provider adapter for transient failures (rate limits, overload,
connection resets). Full jitter — a uniform draw over ``[0, delay]`` rather than
``delay ± noise`` — is what actually breaks up thundering herds when several
runs retry at once.

A ``Retry-After`` value from the provider always wins over the computed delay:
the server knows better than the client when it will be ready.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from tgagent.errors import LLMTransientError
from tgagent.observability.logging import get_logger

log = get_logger(__name__)

T = TypeVar("T")


def compute_delay(
    attempt: int, *, base: float, cap: float, retry_after: float | None = None
) -> float:
    """Delay before *attempt* (1-based), honouring an explicit ``Retry-After``."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, cap)
    exponential = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, exponential)  # noqa: S311 - jitter, not cryptography


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    max_retries: int,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    description: str = "operation",
) -> T:
    """Run *operation*, retrying :class:`LLMTransientError` up to *max_retries* times.

    Non-transient errors propagate immediately — retrying a 400 is pointless and
    just multiplies the latency of a failure the caller needs to see.
    """
    last: LLMTransientError | None = None
    for attempt in range(1, max_retries + 2):
        try:
            return await operation()
        except LLMTransientError as exc:
            last = exc
            if attempt > max_retries:
                break
            delay = compute_delay(
                attempt, base=base_delay, cap=max_delay, retry_after=exc.retry_after
            )
            log.warning(
                "llm.retry",
                description=description,
                attempt=attempt,
                max_retries=max_retries,
                delay_seconds=round(delay, 2),
                reason=str(exc),
            )
            await asyncio.sleep(delay)

    assert last is not None  # loop only exits via return or a captured error
    raise LLMTransientError(
        f"{description} failed after {max_retries + 1} attempts: {last.user_message}",
        retry_after=last.retry_after,
    ) from last
