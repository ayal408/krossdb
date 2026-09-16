"""Circuit breaker: stops hammering a backend that is already failing.

States: CLOSED (normal) -> OPEN (short-circuiting calls) -> HALF_OPEN (probing)
-> CLOSED | OPEN. Thread-safety is provided by an ``asyncio.Lock``; this is
intended for single-event-loop use per breaker instance, which matches one
breaker per adapter/node.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from krossdb.core.config import CircuitBreakerConfig
from krossdb.core.enums import CircuitState
from krossdb.core.exceptions import CircuitBreakerOpenError
from krossdb.core.logging import get_logger

T = TypeVar("T")

_logger = get_logger("krossdb.resilience.circuit_breaker")


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

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Execute ``fn`` under the breaker's protection."""

        await self._before_call()
        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result


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

__all__ = ["CircuitBreaker", "CircuitBreakerRegistry", "registry"]
