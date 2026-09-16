"""MongoDB adapter using Motor's async driver.

Replica-set primary/secondary routing is handled natively by the MongoDB
driver via the connection URI's replica-set members and ``readPreference``
query parameter, so unlike the relational adapter this one does not
reimplement read/write splitting — set ``readPreference`` in the DSN for
replica reads.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from krossdb.core.config import ConnectionConfig
from krossdb.core.enums import DatabaseKind, HealthStatus, RoutingRole
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import ConfigurationError
from krossdb.factory.db_factory import register_adapter
from krossdb.interfaces.adapter import AbstractDatabaseAdapter, HealthCheckResult
from krossdb.resilience.circuit_breaker import CircuitBreaker
from krossdb.resilience.retry import with_retry


class MongoDBAdapter(AbstractDatabaseAdapter[AsyncIOMotorDatabase[dict[str, Any]]]):
    kind = DatabaseKind.MONGODB

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._client: AsyncIOMotorClient[dict[str, Any]] | None = None
        self._database: AsyncIOMotorDatabase[dict[str, Any]] | None = None
        self._breaker = CircuitBreaker(f"{config.name}:mongodb", config.circuit_breaker)

    async def connect(self) -> None:
        master = self.config.master_nodes[0]
        dsn = master.dsn.get_secret_value()
        self._client = AsyncIOMotorClient[dict[str, Any]](
            dsn,
            maxPoolSize=self.config.pool.max_size,
            minPoolSize=self.config.pool.min_size,
            connectTimeoutMS=int(self.config.pool.connect_timeout_seconds * 1000),
        )
        self._database = self._client.get_default_database()
        if self._database is None:
            raise ConfigurationError(
                f"connection {self.config.name!r}: MongoDB DSN must include a default database name"
            )

        client = self._client

        async def _ping() -> None:
            with translate_exceptions(connection=self.config.name, op="connect"):
                await client.admin.command("ping")

        async def _guarded_ping() -> None:
            await with_retry(_ping, config=self.config.retry, operation_name=f"{self.config.name}.ping")

        await self._breaker.call(_guarded_ping)
        self._connected = True

    async def disconnect(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self._database = None
        self._connected = False

    @asynccontextmanager
    async def acquire(self, *, role: RoutingRole = RoutingRole.MASTER) -> AsyncIterator[AsyncIOMotorDatabase[dict[str, Any]]]:
        if self._database is None:
            raise ConfigurationError(f"connection {self.config.name!r} is not connected")
        with translate_exceptions(adapter=str(self.kind), connection=self.config.name):
            yield self._database

    async def health_check(self) -> HealthCheckResult:
        start = time.monotonic()
        if self._client is None:
            return HealthCheckResult(status=HealthStatus.UNHEALTHY, latency_ms=0.0, detail="not connected")
        try:
            with translate_exceptions(connection=self.config.name, op="health_check"):
                await self._client.admin.command("ping")
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


register_adapter(DatabaseKind.MONGODB, MongoDBAdapter)

__all__ = ["MongoDBAdapter"]
