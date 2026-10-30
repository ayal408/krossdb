"""Circuit breaker and retry behavior on the data path (not just connect/health).

Runs the real SQLAlchemy adapter and repository against in-process SQLite;
infrastructure failures are simulated by raising the already-translated
krossdb exceptions from inside the acquired block, exactly where a dropped
connection would surface them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Uuid

from krossdb.adapters.relational.sqlite import SQLiteAdapter
from krossdb.core.config import CircuitBreakerConfig, ConnectionConfig, NodeConfig, RetryConfig
from krossdb.core.enums import CircuitState, DatabaseKind, RoutingRole
from krossdb.core.exceptions import (
    CircuitBreakerOpenError,
    ConnectionError_,
    DuplicateRecordError,
)
from krossdb.models.base import Entity
from krossdb.repositories.relational_repository import SQLAlchemyRepository
from krossdb.uow.sqlalchemy_uow import SQLAlchemyUnitOfWork

pytestmark = pytest.mark.unit

_metadata = MetaData()
_widgets_table = Table(
    "widgets",
    _metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", String, nullable=True),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("version", Integer),
    Column("name", String, unique=True),
    Column("quantity", Integer),
)


class Widget(Entity):
    name: str
    quantity: int = 0


def _config(*, nodes: list[NodeConfig] | None = None, failure_threshold: int = 2) -> ConnectionConfig:
    return ConnectionConfig(
        name="resilient",
        kind=DatabaseKind.SQLITE,
        nodes=nodes or [NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
        retry=RetryConfig(max_attempts=3, initial_backoff_seconds=0.001, max_backoff_seconds=0.01),
        circuit_breaker=CircuitBreakerConfig(
            failure_threshold=failure_threshold, recovery_timeout_seconds=60
        ),
    )


@pytest_asyncio.fixture
async def adapter() -> AsyncIterator[SQLiteAdapter]:
    adapter = SQLiteAdapter(_config())
    await adapter.connect()
    async with adapter.acquire() as session:
        await session.run_sync(lambda sync_session: _metadata.create_all(sync_session.connection()))
    yield adapter
    await adapter.disconnect()


@pytest.fixture
def repo(adapter: SQLiteAdapter) -> SQLAlchemyRepository[Widget, uuid.UUID]:
    return SQLAlchemyRepository(adapter, _widgets_table, Widget)


def _master_breaker(adapter: SQLiteAdapter):
    return adapter._breakers[adapter._select_key(RoutingRole.MASTER)]


def _fail_first_acquires(adapter: SQLiteAdapter, monkeypatch: pytest.MonkeyPatch, failures: int) -> list[int]:
    """Make the first ``failures`` acquire() bodies hit a dropped connection."""

    original_acquire = adapter.acquire
    attempts: list[int] = []

    @asynccontextmanager
    async def flaky_acquire(*, role: RoutingRole = RoutingRole.MASTER):
        attempts.append(1)
        async with original_acquire(role=role) as session:
            if len(attempts) <= failures:
                raise ConnectionError_("connection reset by peer")
            yield session

    monkeypatch.setattr(adapter, "acquire", flaky_acquire)
    return attempts


async def test_repeated_connection_failures_on_queries_open_the_breaker(
    adapter: SQLiteAdapter,
) -> None:
    for _ in range(2):
        with pytest.raises(ConnectionError_):
            async with adapter.acquire():
                raise ConnectionError_("connection reset by peer")

    assert _master_breaker(adapter).state is CircuitState.OPEN
    with pytest.raises(CircuitBreakerOpenError):
        async with adapter.acquire():
            pass


async def test_business_errors_on_queries_do_not_open_the_breaker(
    repo: SQLAlchemyRepository, adapter: SQLiteAdapter
) -> None:
    await repo.create(Widget(name="dup"))
    for _ in range(3):
        with pytest.raises(DuplicateRecordError):
            await repo.create(Widget(name="dup"))

    assert _master_breaker(adapter).state is CircuitState.CLOSED


async def test_reads_are_retried_after_a_transient_failure(
    repo: SQLAlchemyRepository, adapter: SQLiteAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    widget = await repo.create(Widget(name="retry-me", quantity=3))
    attempts = _fail_first_acquires(adapter, monkeypatch, failures=1)

    fetched = await repo.get_by_id(widget.id)

    assert fetched is not None and fetched.quantity == 3
    assert len(attempts) == 2


async def test_find_and_count_are_retried(
    repo: SQLAlchemyRepository, adapter: SQLiteAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    await repo.create(Widget(name="listed"))
    attempts = _fail_first_acquires(adapter, monkeypatch, failures=1)

    assert (await repo.find()).total == 1
    assert await repo.count() == 1
    assert len(attempts) == 3


async def test_writes_are_not_retried(
    repo: SQLAlchemyRepository, adapter: SQLiteAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = _fail_first_acquires(adapter, monkeypatch, failures=1)

    with pytest.raises(ConnectionError_):
        await repo.create(Widget(name="once"))

    assert len(attempts) == 1


async def test_reads_inside_a_unit_of_work_are_not_retried(
    repo: SQLAlchemyRepository, adapter: SQLiteAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with SQLAlchemyUnitOfWork(adapter):
        attempts = _fail_first_acquires(adapter, monkeypatch, failures=1)
        with pytest.raises(ConnectionError_):
            await repo.get_by_id(uuid.uuid4())

    assert len(attempts) == 1


async def test_retries_stop_once_the_breaker_opens(
    repo: SQLAlchemyRepository, adapter: SQLiteAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # threshold 2 < max_attempts 3: the third attempt is short-circuited.
    attempts = _fail_first_acquires(adapter, monkeypatch, failures=10)

    with pytest.raises(CircuitBreakerOpenError):
        await repo.get_by_id(uuid.uuid4())

    assert len(attempts) == 3
    assert _master_breaker(adapter).state is CircuitState.OPEN


async def test_reads_fall_back_to_master_while_the_replica_breaker_is_open(tmp_path) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite'}"
    adapter = SQLiteAdapter(
        _config(
            nodes=[
                NodeConfig(dsn=dsn, role=RoutingRole.MASTER),
                NodeConfig(dsn=dsn, role=RoutingRole.REPLICA),
            ],
            failure_threshold=1,
        )
    )
    await adapter.connect()
    try:
        replica_key = adapter._select_key(RoutingRole.REPLICA)
        assert replica_key != adapter._master_key
        with pytest.raises(ConnectionError_):
            async with adapter._breakers[replica_key].guard():
                raise ConnectionError_("replica down")

        assert adapter._select_key(RoutingRole.REPLICA) == adapter._master_key
        async with adapter.acquire(role=RoutingRole.REPLICA):
            pass
    finally:
        await adapter.disconnect()
