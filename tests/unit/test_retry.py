"""Unit tests for the exponential-backoff retry helper."""

from __future__ import annotations

import pytest

from krossdb.core.config import RetryConfig
from krossdb.core.exceptions import ConnectionError_, IntegrityViolationError
from krossdb.resilience.retry import with_retry

pytestmark = pytest.mark.unit


def fast_retry_config(**overrides) -> RetryConfig:
    defaults = dict(max_attempts=3, initial_backoff_seconds=0.001, max_backoff_seconds=0.01)
    defaults.update(overrides)
    return RetryConfig(**defaults)


async def test_succeeds_on_first_try_without_retrying():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        return "ok"

    result = await with_retry(op, config=fast_retry_config())

    assert result == "ok"
    assert calls == 1


async def test_retries_transient_error_then_succeeds():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError_("transient")
        return "ok"

    result = await with_retry(op, config=fast_retry_config(max_attempts=5))

    assert result == "ok"
    assert calls == 3


async def test_exhausts_attempts_and_reraises():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise ConnectionError_("always fails")

    with pytest.raises(ConnectionError_):
        await with_retry(op, config=fast_retry_config(max_attempts=3))

    assert calls == 3


async def test_non_retryable_error_propagates_immediately():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        raise IntegrityViolationError("constraint violated")

    with pytest.raises(IntegrityViolationError):
        await with_retry(op, config=fast_retry_config(max_attempts=5))

    assert calls == 1  # never retried
