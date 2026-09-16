"""Exponential-backoff retry, built on top of :mod:`tenacity`.

Wraps tenacity so the rest of the codebase depends on our own
:class:`~krossdb.core.config.RetryConfig` model rather than tenacity's API
directly, and so retryable-vs-fatal classification lives in one place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
    wait_random_exponential,
)
from tenacity.wait import wait_base

from krossdb.core.config import RetryConfig
from krossdb.core.exceptions import (
    CircuitBreakerOpenError,
    ConnectionError_,
    ConnectionTimeoutError,
    DeadlockDetectedError,
    KrossDBError,
    TimeoutError_,
)
from krossdb.core.logging import get_logger

T = TypeVar("T")

_logger = get_logger("krossdb.resilience.retry")

# Transient failures worth retrying. Integrity/validation/auth errors are
# deliberately excluded: retrying a rejected constraint or bad credentials
# only delays the inevitable and can mask bugs.
RETRYABLE_EXCEPTIONS: tuple[type[KrossDBError], ...] = (
    ConnectionError_,
    ConnectionTimeoutError,
    TimeoutError_,
    DeadlockDetectedError,
)


def _build_wait(config: RetryConfig) -> wait_base:
    if config.jitter:
        return wait_random_exponential(
            multiplier=config.initial_backoff_seconds, max=config.max_backoff_seconds
        )
    return wait_exponential_jitter(
        initial=config.initial_backoff_seconds,
        max=config.max_backoff_seconds,
        exp_base=config.multiplier,
        jitter=0,
    )


async def with_retry(
    fn: Callable[[], Awaitable[T]],
    *,
    config: RetryConfig | None = None,
    retry_on: tuple[type[Exception], ...] = RETRYABLE_EXCEPTIONS,
    operation_name: str = "operation",
) -> T:
    """Invoke ``fn`` with exponential backoff, retrying only on ``retry_on`` types.

    Never retries :class:`CircuitBreakerOpenError` regardless of ``retry_on``,
    since an open breaker is an explicit "stop calling now" signal.
    """

    cfg = config or RetryConfig()

    async for attempt in AsyncRetrying(
        stop=stop_after_attempt(cfg.max_attempts),
        wait=_build_wait(cfg),
        retry=retry_if_exception_type(retry_on),
        reraise=True,
    ):
        with attempt:
            try:
                return await fn()
            except CircuitBreakerOpenError:
                raise
            except retry_on as exc:
                _logger.warning(
                    "retry.attempt_failed",
                    operation=operation_name,
                    attempt=attempt.retry_state.attempt_number,
                    max_attempts=cfg.max_attempts,
                    error=str(exc),
                )
                raise

    raise AssertionError("unreachable: AsyncRetrying always returns or raises")


__all__ = ["RETRYABLE_EXCEPTIONS", "with_retry"]
