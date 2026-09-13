"""Async retry with exponential backoff + jitter."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Sequence, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff configuration for :func:`retry_async`."""

    attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 15.0
    factor: float = 2.0
    jitter: float = 0.25  # fractional jitter, e.g. 0.25 = ±25%

    def delay_for(self, attempt: int) -> float:
        """Backoff (seconds) before the ``attempt``-th retry (1-indexed)."""
        raw = min(self.max_delay, self.base_delay * (self.factor ** (attempt - 1)))
        if self.jitter:
            raw *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, raw)


class RetryError(RuntimeError):
    """Raised when all retry attempts are exhausted."""

    def __init__(self, message: str, last_exc: BaseException | None = None) -> None:
        super().__init__(message)
        self.last_exc = last_exc


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    retry_on: Sequence[type[BaseException]] = (Exception,),
    give_up_on: Sequence[type[BaseException]] = (),
    on_retry: Callable[[int, BaseException, float], None] | None = None,
    description: str = "operation",
) -> T:
    """Call ``func`` with retries.

    Args:
        func: zero-arg coroutine factory (usually a lambda) to invoke each try.
        policy: backoff policy.
        retry_on: exception types that trigger a retry.
        give_up_on: exception types that abort immediately (checked first).
        on_retry: optional callback ``(attempt, exc, sleep_seconds)`` before sleeping.
        description: label used in the final :class:`RetryError`.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, policy.attempts + 1):
        try:
            return await func()
        except give_up_on:
            raise
        except retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt >= policy.attempts:
                break
            sleep = policy.delay_for(attempt)
            if on_retry is not None:
                on_retry(attempt, exc, sleep)
            await asyncio.sleep(sleep)
    raise RetryError(f"{description} failed after {policy.attempts} attempts", last_exc)
