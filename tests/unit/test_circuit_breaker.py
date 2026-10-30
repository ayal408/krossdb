"""Unit tests for the circuit breaker's state machine."""

from __future__ import annotations

import asyncio

import pytest

from krossdb.core.config import CircuitBreakerConfig
from krossdb.core.enums import CircuitState
from krossdb.core.exceptions import CircuitBreakerOpenError
from krossdb.resilience.circuit_breaker import CircuitBreaker

pytestmark = pytest.mark.unit


def make_breaker(**overrides) -> CircuitBreaker:
    config = CircuitBreakerConfig(
        failure_threshold=overrides.pop("failure_threshold", 3),
        recovery_timeout_seconds=overrides.pop("recovery_timeout_seconds", 0.05),
        half_open_max_calls=overrides.pop("half_open_max_calls", 1),
    )
    return CircuitBreaker("test-breaker", config)


async def test_starts_closed():
    breaker = make_breaker()
    assert breaker.state is CircuitState.CLOSED


async def test_successful_calls_keep_it_closed():
    breaker = make_breaker()

    async def ok():
        return "fine"

    for _ in range(10):
        assert await breaker.call(ok) == "fine"
    assert breaker.state is CircuitState.CLOSED


async def test_opens_after_reaching_failure_threshold():
    breaker = make_breaker(failure_threshold=3)

    async def failing():
        raise ValueError("nope")

    for _ in range(3):
        with pytest.raises(ValueError):
            await breaker.call(failing)

    assert breaker.state is CircuitState.OPEN


async def test_open_breaker_short_circuits_without_calling_fn():
    breaker = make_breaker(failure_threshold=1)
    calls = 0

    async def failing():
        nonlocal calls
        calls += 1
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await breaker.call(failing)
    assert breaker.state is CircuitState.OPEN

    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call(failing)

    assert calls == 1  # second call was short-circuited, fn never invoked


async def test_transitions_to_half_open_after_recovery_timeout_and_closes_on_success():
    breaker = make_breaker(failure_threshold=1, recovery_timeout_seconds=0.02)

    async def failing():
        raise ValueError("nope")

    async def ok():
        return "recovered"

    with pytest.raises(ValueError):
        await breaker.call(failing)
    assert breaker.state is CircuitState.OPEN

    await asyncio.sleep(0.03)

    result = await breaker.call(ok)

    assert result == "recovered"
    assert breaker.state is CircuitState.CLOSED


async def test_failure_during_half_open_reopens_immediately():
    breaker = make_breaker(failure_threshold=1, recovery_timeout_seconds=0.02)

    async def failing():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await breaker.call(failing)
    await asyncio.sleep(0.03)

    with pytest.raises(ValueError):
        await breaker.call(failing)

    assert breaker.state is CircuitState.OPEN


async def test_guard_with_failure_types_ignores_non_infrastructure_errors():
    from krossdb.core.exceptions import IntegrityViolationError
    from krossdb.resilience.circuit_breaker import INFRASTRUCTURE_FAILURES

    breaker = make_breaker(failure_threshold=1)

    for _ in range(3):
        with pytest.raises(IntegrityViolationError):
            async with breaker.guard(failure_types=INFRASTRUCTURE_FAILURES):
                raise IntegrityViolationError("duplicate key")

    assert breaker.state is CircuitState.CLOSED


async def test_guard_opens_on_infrastructure_errors_and_then_rejects_entry():
    from krossdb.core.exceptions import ConnectionError_
    from krossdb.resilience.circuit_breaker import INFRASTRUCTURE_FAILURES

    breaker = make_breaker(failure_threshold=2, recovery_timeout_seconds=60)
    for _ in range(2):
        with pytest.raises(ConnectionError_):
            async with breaker.guard(failure_types=INFRASTRUCTURE_FAILURES):
                raise ConnectionError_("connection reset")

    assert breaker.state is CircuitState.OPEN
    assert not breaker.allows_calls
    entered = False
    with pytest.raises(CircuitBreakerOpenError):
        async with breaker.guard(failure_types=INFRASTRUCTURE_FAILURES):
            entered = True
    assert not entered


async def test_half_open_probe_answered_with_a_business_error_closes_the_breaker():
    from krossdb.core.exceptions import ConnectionError_, RecordNotFoundError
    from krossdb.resilience.circuit_breaker import INFRASTRUCTURE_FAILURES

    breaker = make_breaker(failure_threshold=1, recovery_timeout_seconds=0.01)
    with pytest.raises(ConnectionError_):
        async with breaker.guard(failure_types=INFRASTRUCTURE_FAILURES):
            raise ConnectionError_("down")
    await asyncio.sleep(0.02)
    assert breaker.allows_calls

    with pytest.raises(RecordNotFoundError):
        async with breaker.guard(failure_types=INFRASTRUCTURE_FAILURES):
            raise RecordNotFoundError("Widget", "missing")

    assert breaker.state is CircuitState.CLOSED


async def test_cancelled_half_open_probe_frees_its_slot():
    breaker = make_breaker(failure_threshold=1, recovery_timeout_seconds=0.01, half_open_max_calls=1)

    async def failing():
        raise ValueError("down")

    with pytest.raises(ValueError):
        await breaker.call(failing)
    await asyncio.sleep(0.02)

    probe = asyncio.create_task(breaker.call(asyncio.Event().wait))
    await asyncio.sleep(0)
    assert breaker.state is CircuitState.HALF_OPEN
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    async def ok():
        return "recovered"

    assert await breaker.call(ok) == "recovered"
    assert breaker.state is CircuitState.CLOSED
