"""Circuit breaker: stops hammering a backend that is already failing.

States: CLOSED (normal) -> OPEN (short-circuiting calls) -> HALF_OPEN (probing)
-> CLOSED | OPEN. Thread-safety is provided by an ``asyncio.Lock``; this is
intended for single-event-loop use per breaker instance, which matches one
breaker per adapter/node.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

from krossdb.core.config import CircuitBreakerConfig
from krossdb.core.enums import CircuitState
from krossdb.core.exceptions import (
    CircuitBreakerOpenError,
    ConnectionError_,
    ConnectionTimeoutError,
    TimeoutError_,
)
from krossdb.core.logging import get_logger

T = TypeVar("T")

_logger = get_logger("krossdb.resilience.circuit_breaker")

# Failures that say the dependency itself is unhealthy. Anything else raised
# through a guarded block (a constraint violation, a missing record, a bug in
# the caller) means the backend answered, so it counts as a success.
INFRASTRUCTURE_FAILURES: tuple[type[Exception], ...] = (
    ConnectionError_,
    ConnectionTimeoutError,
    TimeoutError_,
)


class CircuitBreaker:
    """A single breaker guarding one dependency (e.g. one database node)."""

    def __init__(self, name: str, config: CircuitBreakerConfig | None = None) -> None:
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None
        self._half_open_calls_in_flight = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def allows_calls(self) -> bool:
        """Whether a call made now would be let through (best-effort, lock-free).

        Used for routing decisions such as skipping an open replica; the
        authoritative check still happens when the call enters :meth:`guard`.
        """

        if self._state is not CircuitState.OPEN:
            return True
        assert self._opened_at is not None
        return time.monotonic() - self._opened_at >= self.config.recovery_timeout_seconds

    async def _transition(self, new_state: CircuitState) -> None:
        if new_state == self._state:
            return
        _logger.info("circuit_breaker.transition", breaker=self.name, frm=self._state, to=new_state)
        self._state = new_state
        if new_state is CircuitState.OPEN:
            self._opened_at = time.monotonic()
        elif new_state is CircuitState.CLOSED:
            self._failure_count = 0
            self._opened_at = None
        elif new_state is CircuitState.HALF_OPEN:
            self._half_open_calls_in_flight = 0

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is CircuitState.OPEN:
                assert self._opened_at is not None
                elapsed = time.monotonic() - self._opened_at
                if elapsed >= self.config.recovery_timeout_seconds:
                    await self._transition(CircuitState.HALF_OPEN)
                else:
                    raise CircuitBreakerOpenError(
                        f"Circuit '{self.name}' is open; retry in "
                        f"{self.config.recovery_timeout_seconds - elapsed:.1f}s",
                        context={"breaker": self.name, "state": str(self._state)},
                    )

            if self._state is CircuitState.HALF_OPEN:
                if self._half_open_calls_in_flight >= self.config.half_open_max_calls:
                    raise CircuitBreakerOpenError(
                        f"Circuit '{self.name}' is half-open and at its probe limit",
                        context={"breaker": self.name, "state": str(self._state)},
                    )
                self._half_open_calls_in_flight += 1

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                await self._transition(CircuitState.CLOSED)
            elif self._state is CircuitState.CLOSED:
                self._failure_count = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                await self._transition(CircuitState.OPEN)
                return
            self._failure_count += 1
            if self._failure_count >= self.config.failure_threshold:
                await self._transition(CircuitState.OPEN)

    async def _on_abandoned(self) -> None:
        # A probe that was cancelled never reported back; free its slot so the
        # breaker can't get stuck half-open at its probe limit.
        async with self._lock:
            if self._state is CircuitState.HALF_OPEN and self._half_open_calls_in_flight > 0:
                self._half_open_calls_in_flight -= 1

    @asynccontextmanager
    async def guard(
        self, *, failure_types: tuple[type[Exception], ...] | None = None
    ) -> AsyncIterator[None]:
        """Run the enclosed block under the breaker's protection.

        Raises :class:`CircuitBreakerOpenError` on entry while the circuit is
        open. ``failure_types`` limits which exceptions count as failures
        (e.g. :data:`INFRASTRUCTURE_FAILURES`); any other exception is
        re-raised but recorded as a success. ``None`` counts every exception.
        """

        await self._before_call()
        try:
            yield
        except Exception as exc:
            if failure_types is None or isinstance(exc, failure_types):
                await self._on_failure()
            else:
                await self._on_success()
            raise
        except BaseException:
            await self._on_abandoned()
            raise
        else:
            await self._on_success()

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Execute ``fn`` under the breaker's protection; every exception counts as a failure."""

        async with self.guard():
            return await fn()


class CircuitBreakerRegistry:
    """Process-wide keyed registry so adapters share one breaker per node/dependency."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_or_create(self, name: str, config: CircuitBreakerConfig | None = None) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(name, config)
        return self._breakers[name]

    def all(self) -> dict[str, CircuitBreaker]:
        return dict(self._breakers)


registry = CircuitBreakerRegistry()

__all__ = ["INFRASTRUCTURE_FAILURES", "CircuitBreaker", "CircuitBreakerRegistry", "registry"]
