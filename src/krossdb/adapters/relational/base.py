"""SQLAlchemy-async-backed base adapter shared by Postgres/MySQL/SQLite.

Each configured node gets its own pooled :class:`AsyncEngine`. Writes always
route to the single master node; reads route to a weighted-round-robin
replica pool when replicas are configured, falling back to master otherwise.
The master node's connectivity is verified eagerly on :meth:`connect` (through
retry + circuit breaker) since a missing master is a startup-time
configuration problem; replicas are verified lazily (SQLAlchemy's
``pool_pre_ping``) since a temporarily down replica shouldn't block startup.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from krossdb.adapters.relational.routing import LoadBalancer
from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import HealthStatus, RoutingRole
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import CircuitBreakerOpenError, ConfigurationError
from krossdb.interfaces.adapter import AbstractDatabaseAdapter, HealthCheckResult
from krossdb.resilience.circuit_breaker import CircuitBreaker
from krossdb.resilience.retry import with_retry


class SQLAlchemyAdapter(AbstractDatabaseAdapter[AsyncSession]):
    """Base adapter for any SQLAlchemy-async-supported relational database."""

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._engines: dict[str, AsyncEngine] = {}
        self._session_factories: dict[str, async_sessionmaker[AsyncSession]] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._replica_balancer: LoadBalancer | None = None
        self._master_key: str | None = None
        # Task-local override so a Unit of Work can pin every acquire() within
        # its transaction to one session without mutating shared adapter state
        # (a plain instance attribute would race across concurrent tasks).
        self._active_session: ContextVar[AsyncSession | None] = ContextVar(
            f"krossdb_active_session_{id(self)}", default=None
        )

    @staticmethod
    def _node_key(config_name: str, node: NodeConfig, index: int) -> str:
        return f"{config_name}:{node.role}:{index}"

    def _engine_kwargs(self, dsn: str) -> dict[str, object]:
        # SQLite in-memory databases default to StaticPool, which (unlike
        # QueuePool/AsyncAdaptedQueuePool) doesn't accept pool_size/max_overflow,
        # and pre-ping is meaningless for an in-process, non-networked database.
        if dsn.startswith("sqlite") and ":memory:" in dsn:
            return {"echo": self.config.echo}
        return {
            "echo": self.config.echo,
            "pool_size": self.config.pool.max_size,
            "max_overflow": self.config.pool.max_overflow,
            "pool_recycle": int(self.config.pool.pool_recycle_seconds),
            "pool_pre_ping": True,
        }

    async def connect(self) -> None:
        for index, node in enumerate(self.config.nodes):
            key = self._node_key(self.config.name, node, index)
            dsn = node.dsn.get_secret_value()
            engine = create_async_engine(dsn, **self._engine_kwargs(dsn))
            self._engines[key] = engine
            self._session_factories[key] = async_sessionmaker(engine, expire_on_commit=False)
            self._breakers[key] = CircuitBreaker(key, self.config.circuit_breaker)

            if node.role is RoutingRole.MASTER and self._master_key is None:
                self._master_key = key

        if self._master_key is None:
            raise ConfigurationError(f"connection {self.config.name!r} has no master node")

        replica_entries = [
            (self._node_key(self.config.name, n, i), n.weight)
            for i, n in enumerate(self.config.nodes)
            if n.role is RoutingRole.REPLICA
        ]
        if replica_entries:
            keys, weights = zip(*replica_entries, strict=True)
            self._replica_balancer = LoadBalancer(list(keys), list(weights))

        await self._verify_node(self._master_key)
        self._connected = True

    async def disconnect(self) -> None:
        for engine in self._engines.values():
            await engine.dispose()
        self._engines.clear()
        self._session_factories.clear()
        self._breakers.clear()
        self._replica_balancer = None
        self._master_key = None
        self._connected = False

    async def _ping(self, key: str) -> None:
        async def _do_ping() -> None:
            async with self._engines[key].connect() as conn:
                await conn.execute(text("SELECT 1"))

        await with_retry(_do_ping, config=self.config.retry, operation_name=f"{self.config.name}.ping[{key}]")

    async def _verify_node(self, key: str) -> None:
        breaker = self._breakers[key]

        async def _guarded() -> None:
            await self._ping(key)

        await breaker.call(_guarded)

    def _select_key(self, role: RoutingRole) -> str:
        if role is RoutingRole.MASTER or self._replica_balancer is None:
            if self._master_key is None:
                raise ConfigurationError(f"connection {self.config.name!r} is not connected")
            return self._master_key
        return self._replica_balancer.next()

    def bind_session(self, session: AsyncSession) -> Token[AsyncSession | None]:
        """Pin every subsequent ``acquire()`` in this task to ``session`` (used by the UoW)."""

        return self._active_session.set(session)

    def unbind_session(self, token: Token[AsyncSession | None]) -> None:
        self._active_session.reset(token)

    @asynccontextmanager
    async def acquire(self, *, role: RoutingRole = RoutingRole.MASTER) -> AsyncIterator[AsyncSession]:
        """Yield a session for one repository call.

        Outside a Unit of Work, each ``acquire()`` owns its own transaction:
        it commits automatically on a clean exit and rolls back on exception,
        so standalone repository calls behave like autocommit. Inside a
        :class:`~krossdb.uow.sqlalchemy_uow.SQLAlchemyUnitOfWork`, the bound
        session is handed back unchanged and neither committed nor rolled
        back here — the UoW alone governs that transaction's lifecycle.
        """

        bound = self._active_session.get()
        if bound is not None:
            with translate_exceptions(adapter=str(self.config.kind), connection=self.config.name):
                yield bound
            return

        key = self._select_key(role)
        session = self._session_factories[key]()
        try:
            try:
                with translate_exceptions(adapter=str(self.config.kind), connection=self.config.name, node=key):
                    yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
        finally:
            await session.close()

    async def health_check(self) -> HealthCheckResult:
        start = time.monotonic()
        if self._master_key is None or self._master_key not in self._engines:
            return HealthCheckResult(status=HealthStatus.UNHEALTHY, latency_ms=0.0, detail="not connected")

        try:
            await self._verify_node(self._master_key)
        except CircuitBreakerOpenError as exc:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                latency_ms=(time.monotonic() - start) * 1000,
                detail=str(exc),
                checked_nodes=len(self._engines),
            )
        except Exception as exc:
            return HealthCheckResult(
                status=HealthStatus.UNHEALTHY,
                latency_ms=(time.monotonic() - start) * 1000,
                detail=str(exc),
                checked_nodes=len(self._engines),
            )

        replica_down = 0
        for key in list(self._engines):
            if key == self._master_key:
                continue
            try:
                async with self._engines[key].connect() as conn:
                    await conn.execute(text("SELECT 1"))
            except Exception:
                replica_down += 1

        latency_ms = (time.monotonic() - start) * 1000
        status = HealthStatus.DEGRADED if replica_down else HealthStatus.HEALTHY
        detail = f"{replica_down} replica(s) unreachable" if replica_down else ""
        return HealthCheckResult(
            status=status, latency_ms=latency_ms, detail=detail, checked_nodes=len(self._engines)
        )


__all__ = ["SQLAlchemyAdapter"]
