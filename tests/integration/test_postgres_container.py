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

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Uuid, text

pytest.importorskip("asyncpg")
pytest.importorskip("sqlalchemy.ext.asyncio")

from nexusdb.adapters.relational.postgres import PostgresAdapter
from nexusdb.core.config import ConnectionConfig, NodeConfig
from nexusdb.core.enums import DatabaseKind, RoutingRole
from nexusdb.core.exception_mapper import translate_exceptions
from nexusdb.core.exceptions import DuplicateRecordError, RecordNotFoundError
from nexusdb.models.base import Entity
from nexusdb.repositories.relational_repository import SQLAlchemyRepository
from nexusdb.uow.sqlalchemy_uow import SQLAlchemyUnitOfWork

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
