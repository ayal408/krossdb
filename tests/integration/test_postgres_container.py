"""Integration tests against a real Postgres container (via Testcontainers).

Two layers: a raw-SQLAlchemy smoke test proving the fixture/exception-mapper
wiring works, and full end-to-end tests of ``PostgresAdapter`` +
``SQLAlchemyRepository`` + ``SQLAlchemyUnitOfWork`` against the real database
— the same code path exercised against SQLite in
tests/unit/test_relational_repository_sqlite.py, here proving it also works
against real Postgres wire behavior (real constraint errors, real
transactions).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, Uuid, select, text

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy.ext.asyncio")

from krossdb.adapters.relational.postgres import PostgresAdapter
from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import DatabaseKind, RoutingRole
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import DuplicateRecordError, RecordNotFoundError
from krossdb.events.outbox import SQLAlchemyOutboxStore
from krossdb.events.relay import OutboxRelay
from krossdb.interfaces.events import DomainEvent, OutboxRecord
from krossdb.models.base import Entity
from krossdb.repositories.relational_repository import SQLAlchemyRepository
from krossdb.uow.sqlalchemy_uow import SQLAlchemyUnitOfWork

_metadata = MetaData()
_widgets_table = Table(
    "widgets_pg_it",
    _metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", String, nullable=True),
    # timezone=True: Entity.created_at/updated_at are tz-aware (datetime.now(UTC));
    # asyncpg rejects binding a tz-aware value into a naive TIMESTAMP column.
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("version", Integer),
    Column("name", String, unique=True),
    Column("quantity", Integer),
)

_outbox_table = Table(
    "outbox_pg_it",
    _metadata,
    Column("id", String, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False),
)

# Separate from _outbox_table: other tests leave unpublished rows there.
_relay_outbox_table = Table(
    "outbox_relay_pg_it",
    _metadata,
    Column("id", String, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False),
)


class WidgetCreated(DomainEvent):
    widget_id: str


class Widget(Entity):
    name: str
    quantity: int = 0


async def test_can_connect_and_run_a_query(postgres_dsn: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_dsn)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


async def test_integrity_violation_is_mapped_to_duplicate_record_error(postgres_dsn: str) -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(postgres_dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("CREATE TABLE IF NOT EXISTS raw_widgets_it (id INT PRIMARY KEY)")
            )
            await conn.execute(
                text("INSERT INTO raw_widgets_it (id) VALUES (1) ON CONFLICT DO NOTHING")
            )

        with pytest.raises(DuplicateRecordError):
            async with engine.begin() as conn:
                with translate_exceptions(op="insert"):
                    await conn.execute(text("INSERT INTO raw_widgets_it (id) VALUES (1)"))
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def pg_adapter(postgres_dsn: str) -> AsyncIterator[PostgresAdapter]:
    config = ConnectionConfig(
        name="pg-it",
        kind=DatabaseKind.POSTGRESQL,
        nodes=[NodeConfig(dsn=postgres_dsn, role=RoutingRole.MASTER)],
    )
    adapter = PostgresAdapter(config)
    await adapter.connect()
    async with adapter.acquire() as session:
        await session.run_sync(lambda s: _metadata.create_all(s.connection(), checkfirst=True))
    yield adapter
    await adapter.disconnect()


@pytest.fixture
def widget_repo(pg_adapter: PostgresAdapter) -> SQLAlchemyRepository:
    return SQLAlchemyRepository(pg_adapter, _widgets_table, Widget)


async def test_repository_crud_round_trip(widget_repo: SQLAlchemyRepository) -> None:
    widget = Widget(name=f"alpha-{uuid.uuid4()}", quantity=1)

    await widget_repo.create(widget)
    fetched = await widget_repo.get_by_id(widget.id)
    assert fetched is not None and fetched.name == widget.name

    updated = await widget_repo.update(widget.id, {"quantity": 9})
    assert updated.quantity == 9

    assert await widget_repo.delete(widget.id) is True
    assert await widget_repo.get_by_id(widget.id) is None


async def test_repository_unique_violation_maps_to_duplicate_record_error(
    widget_repo: SQLAlchemyRepository,
) -> None:
    name = f"dup-{uuid.uuid4()}"
    await widget_repo.create(Widget(name=name, quantity=1))

    with pytest.raises(DuplicateRecordError):
        await widget_repo.create(Widget(name=name, quantity=2))


async def test_repository_update_missing_raises_record_not_found(
    widget_repo: SQLAlchemyRepository,
) -> None:
    with pytest.raises(RecordNotFoundError):
        await widget_repo.update(uuid.uuid4(), {"quantity": 1})


async def test_bulk_create_partial_success_against_real_postgres(
    widget_repo: SQLAlchemyRepository,
) -> None:
    name = f"bulk-dup-{uuid.uuid4()}"
    w1 = Widget(name=name, quantity=1)
    w2 = Widget(name=name, quantity=2)  # unique-constraint collision
    w3 = Widget(name=f"bulk-ok-{uuid.uuid4()}", quantity=3)

    result = await widget_repo.bulk_create([w1, w2, w3])

    assert result.success_count == 2
    assert result.failure_count == 1
    assert await widget_repo.get_by_id(w1.id) is not None
    assert await widget_repo.get_by_id(w3.id) is not None


async def test_uow_commit_and_rollback_against_real_postgres(
    pg_adapter: PostgresAdapter, widget_repo: SQLAlchemyRepository
) -> None:
    committed = Widget(name=f"committed-{uuid.uuid4()}", quantity=1)
    rolled_back = Widget(name=f"rolledback-{uuid.uuid4()}", quantity=1)

    async with SQLAlchemyUnitOfWork(pg_adapter) as uow:
        await widget_repo.create(committed)
        await uow.commit()
    assert await widget_repo.get_by_id(committed.id) is not None

    async with SQLAlchemyUnitOfWork(pg_adapter) as uow:
        await widget_repo.create(rolled_back)
        await uow.rollback()
    assert await widget_repo.get_by_id(rolled_back.id) is None


async def test_health_check_against_real_postgres(pg_adapter: PostgresAdapter) -> None:
    result = await pg_adapter.health_check()

    assert str(result.status) == "healthy"


async def test_uow_outbox_event_and_business_row_commit_atomically(
    pg_adapter: PostgresAdapter, widget_repo: SQLAlchemyRepository
) -> None:
    widget = Widget(name=f"outbox-commit-{uuid.uuid4()}", quantity=1)
    event = WidgetCreated(widget_id=str(widget.id))
    store = SQLAlchemyOutboxStore(pg_adapter, _outbox_table)

    async with SQLAlchemyUnitOfWork(pg_adapter, outbox_store=store) as uow:
        await widget_repo.create(widget)
        uow.register_event(event)
        await uow.commit()

    assert await widget_repo.get_by_id(widget.id) is not None
    async with pg_adapter.acquire() as session:
        row = (await session.execute(
            select(_outbox_table).where(_outbox_table.c.id == str(event.event_id))
        )).mappings().one()
    assert row["event_type"] == "WidgetCreated"
    assert str(widget.id) in row["payload"]


async def test_uow_outbox_event_and_business_row_roll_back_atomically(
    pg_adapter: PostgresAdapter, widget_repo: SQLAlchemyRepository
) -> None:
    widget = Widget(name=f"outbox-rollback-{uuid.uuid4()}", quantity=1)
    event = WidgetCreated(widget_id=str(widget.id))
    store = SQLAlchemyOutboxStore(pg_adapter, _outbox_table)

    async with SQLAlchemyUnitOfWork(pg_adapter, outbox_store=store) as uow:
        await widget_repo.create(widget)
        uow.register_event(event)
        await uow.rollback()

    assert await widget_repo.get_by_id(widget.id) is None
    async with pg_adapter.acquire() as session:
        rows = (await session.execute(
            select(_outbox_table).where(_outbox_table.c.id == str(event.event_id))
        )).all()
    assert rows == []


async def test_concurrent_relays_publish_disjoint_batches(pg_adapter: PostgresAdapter) -> None:
    store = SQLAlchemyOutboxStore(pg_adapter, _relay_outbox_table)
    async with pg_adapter.acquire() as session:
        await session.execute(_relay_outbox_table.delete())
    await store.save([OutboxRecord(event_type="WidgetCreated", payload={"i": i}) for i in range(6)])

    first_claimed = asyncio.Event()
    release_first = asyncio.Event()
    published_a: list[int] = []
    published_b: list[int] = []

    async def publish_a(record: OutboxRecord) -> None:
        published_a.append(record.payload["i"])
        first_claimed.set()
        await release_first.wait()

    async def publish_b(record: OutboxRecord) -> None:
        published_b.append(record.payload["i"])

    relay_a = asyncio.create_task(OutboxRelay(store, publish_a, batch_size=3).run_once())
    await asyncio.wait_for(first_claimed.wait(), timeout=10)
    # Relay A still holds its batch's row locks. Without SKIP LOCKED, B would
    # either block on them (timing out here) or publish the same records.
    await asyncio.wait_for(OutboxRelay(store, publish_b, batch_size=3).run_once(), timeout=10)
    release_first.set()
    await asyncio.wait_for(relay_a, timeout=10)

    assert not set(published_a) & set(published_b)
    assert sorted(published_a + published_b) == list(range(6))
    assert await store.fetch_unpublished() == []
