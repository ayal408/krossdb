"""Qdrant adapter for vector/embedding workloads."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from qdrant_client import AsyncQdrantClient

from krossdb.core.config import ConnectionConfig
from krossdb.core.enums import DatabaseKind, HealthStatus, RoutingRole
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import ConfigurationError
from krossdb.factory.db_factory import register_adapter
from krossdb.interfaces.adapter import AbstractDatabaseAdapter, HealthCheckResult
from krossdb.resilience.circuit_breaker import CircuitBreaker
from krossdb.resilience.retry import with_retry


class QdrantAdapter(AbstractDatabaseAdapter[AsyncQdrantClient]):
    kind = DatabaseKind.QDRANT

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._client: AsyncQdrantClient | None = None
        self._breaker = CircuitBreaker(f"{config.name}:qdrant", config.circuit_breaker)

    async def connect(self) -> None:
        master = self.config.master_nodes[0]
        url = master.dsn.get_secret_value()
        self._client = AsyncQdrantClient(
            url=url,
            timeout=int(self.config.pool.connect_timeout_seconds),
            # The client/server compatibility check runs in a background
            # thread and only ever warns; letting it run under a strict
            # `warnings.filterwarnings("error")` policy (common in test
            # suites) turns a harmless version-skew notice into a crash.
            # Version compatibility is the deployer's responsibility.
            check_compatibility=False,
        )

        client = self._client

        async def _ping() -> None:
            with translate_exceptions(connection=self.config.name, op="connect"):
                await client.get_collections()

        async def _guarded_ping() -> None:
            await with_retry(_ping, config=self.config.retry, operation_name=f"{self.config.name}.ping")

        await self._breaker.call(_guarded_ping)
        self._connected = True

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.close()
        self._client = None
        self._connected = False

    @asynccontextmanager
    async def acquire(self, *, role: RoutingRole = RoutingRole.MASTER) -> AsyncIterator[AsyncQdrantClient]:
        if self._client is None:
            raise ConfigurationError(f"connection {self.config.name!r} is not connected")
        with translate_exceptions(adapter=str(self.kind), connection=self.config.name):
            yield self._client

    async def health_check(self) -> HealthCheckResult:
        start = time.monotonic()
        if self._client is None:
            return HealthCheckResult(status=HealthStatus.UNHEALTHY, latency_ms=0.0, detail="not connected")
        try:
            with translate_exceptions(connection=self.config.name, op="health_check"):
                await self._client.get_collections()
            return HealthCheckResult(
                status=HealthStatus.HEALTHY, latency_ms=(time.monotonic() - start) * 1000, checked_nodes=1
            )
        except Exception as exc:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                latency_ms=(time.monotonic() - start) * 1000,
                detail=str(exc),
                checked_nodes=1,
            )


register_adapter(DatabaseKind.QDRANT, QdrantAdapter)

__all__ = ["QdrantAdapter"]
