"""Abstract adapter contract every database backend must implement.

An adapter owns connection lifecycle, pooling, read/write routing, and health
reporting for exactly one logical database (see
:class:`krossdb.core.config.ConnectionConfig`). It deliberately knows nothing
about the repository/domain layer above it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from krossdb.core.config import ConnectionConfig
from krossdb.core.enums import DatabaseKind, HealthStatus, RoutingRole
from krossdb.core.logging import get_logger

TConnection = TypeVar("TConnection")
"""The native connection/session/client type a concrete adapter wraps
(e.g. ``AsyncSession`` for SQLAlchemy, ``AsyncIOMotorDatabase`` for MongoDB,
``AsyncQdrantClient`` for Qdrant)."""


class HealthCheckResult(BaseModel):
    """Outcome of an adapter's health probe."""

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    latency_ms: float = Field(ge=0)
    detail: str = ""
    checked_nodes: int = 0


class AbstractDatabaseAdapter(ABC, Generic[TConnection]):
    """Base class for all database adapters (relational, document, vector).

    Lifecycle: ``connect()`` -> repeated ``acquire()`` -> ``disconnect()``.
    Adapters are expected to be long-lived, process-scoped objects created by
    :class:`krossdb.factory.db_factory.DatabaseFactory`, not created per-request.
    """

    kind: DatabaseKind

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config
        self._connected = False
        self._logger = get_logger(f"krossdb.adapter.{config.kind}.{config.name}")

    # -- lifecycle ---------------------------------------------------------

    @abstractmethod
    async def connect(self) -> None:
        """Establish connection pool(s) for every configured node."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Gracefully drain and close every connection pool."""

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def __aenter__(self) -> AbstractDatabaseAdapter[TConnection]:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    # -- connection acquisition / routing -----------------------------------

    @abstractmethod
    def acquire(
        self, *, role: RoutingRole = RoutingRole.MASTER
    ) -> AbstractAsyncContextManager[TConnection]:
        """Acquire a native connection.

        ``role=MASTER`` always routes to a write-capable node. ``role=REPLICA``
        routes to a read replica when the adapter/config has one configured
        and falls back to master otherwise (never the other way around).
        """

    # -- observability -------------------------------------------------------

    @abstractmethod
    async def health_check(self) -> HealthCheckResult:
        """Probe connectivity/latency without raising; failures are reflected in
        the returned status, not exceptions, so callers can build dashboards
        or readiness endpoints without wrapping every call in try/except."""

    async def health_stream(self, *, interval_seconds: float = 30.0) -> AsyncIterator[HealthCheckResult]:
        """Convenience generator for periodic health polling (e.g. by a sidecar)."""

        import asyncio

        while True:
            yield await self.health_check()
            await asyncio.sleep(interval_seconds)


__all__ = ["AbstractDatabaseAdapter", "HealthCheckResult", "TConnection"]
