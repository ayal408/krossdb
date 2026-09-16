"""End-to-end krossdb quickstart: define a model, wire up a SQLite-backed
repository through the DatabaseFactory, and exercise CRUD + a transactional
Unit of Work.

Runs with no external services (SQLite is embedded), so it doubles as a
smoke test of the public API surface:

    pip install -e ".[sqlite]"
    python examples/quickstart.py
"""

from __future__ import annotations

import asyncio

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Uuid

from krossdb import (
    ConnectionConfig,
    DatabaseFactory,
    DatabaseKind,
    Entity,
    KrossDBError,
    NodeConfig,
    RoutingRole,
    configure_logging,
)
from krossdb.repositories.relational_repository import SQLAlchemyRepository
from krossdb.uow.sqlalchemy_uow import SQLAlchemyUnitOfWork


# 1. Define the domain entity as a plain Pydantic model.
class Widget(Entity):
    name: str
    quantity: int = 0


# 2. Declare the backing table with SQLAlchemy Core (no ORM mapping needed).
#    Use timezone-aware DateTime columns — Entity's timestamps are tz-aware.
metadata = MetaData()
widgets_table = Table(
    "widgets",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", String, nullable=True),
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("version", Integer),
    Column("name", String, unique=True),
    Column("quantity", Integer),
)


async def main() -> None:
    configure_logging(json_output=False)

    config = ConnectionConfig(
        name="primary",
        kind=DatabaseKind.SQLITE,
        nodes=[NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
    )

    async with DatabaseFactory([config]) as factory:
        adapter = factory.get("primary")

        # Create the table (one-time schema setup; the CLI's `krossdb schema
        # create` command does this against a real deployment).
        async with adapter.acquire() as session:
            await session.run_sync(lambda s: metadata.create_all(s.connection()))

        repo = SQLAlchemyRepository(adapter, widgets_table, Widget)

        # --- basic CRUD -----------------------------------------------------
        widget = await repo.create(Widget(name="gizmo", quantity=10))
        print(f"created: {widget}")

        fetched = await repo.get_by_id_or_raise(widget.id)
        print(f"fetched: {fetched}")

        updated = await repo.update(widget.id, {"quantity": 25})
        print(f"updated: {updated}")

        # --- exceptions are always KrossDBError subclasses -------------------
        try:
            await repo.create(Widget(name="gizmo", quantity=1))  # duplicate name
        except KrossDBError as exc:
            print(f"expected failure: {type(exc).__name__}: {exc}")

        # --- an atomic transaction spanning multiple writes -------------------
        async with SQLAlchemyUnitOfWork(adapter) as uow:
            await repo.create(Widget(name="widget-a", quantity=1))
            await repo.create(Widget(name="widget-b", quantity=2))
            await uow.commit()
        print("committed two widgets atomically")

        page = await repo.find(limit=10)
        print(f"total widgets: {page.total}")

        health = await adapter.health_check()
        print(f"adapter health: {health.status}")


if __name__ == "__main__":
    asyncio.run(main())
