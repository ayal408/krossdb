"""Verify outbox round trips for SQLAlchemy JSON and TEXT payload columns."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text

from krossdb.adapters.relational.sqlite import SQLiteAdapter
from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import DatabaseKind, RoutingRole
from krossdb.events.outbox import SQLAlchemyOutboxStore
from krossdb.interfaces.events import OutboxRecord

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("payload_type", [JSON, Text])
async def test_outbox_payload_round_trip_with_json_or_text_column(payload_type):
    metadata = MetaData()
    table = Table(
        "outbox_payload_test", metadata,
        Column("id", String, primary_key=True),
        Column("event_type", String, nullable=False),
        Column("payload", payload_type, nullable=False),
        Column("created_at", DateTime, nullable=False),
        Column("published_at", DateTime, nullable=True),
        Column("attempts", Integer, nullable=False),
    )
    adapter = SQLiteAdapter(ConnectionConfig(
        name="outbox-payload-test", kind=DatabaseKind.SQLITE,
        nodes=[NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
    ))
    await adapter.connect()
    try:
        async with adapter.acquire() as session:
            await session.run_sync(lambda sync: metadata.create_all(sync.connection()))
        store = SQLAlchemyOutboxStore(adapter, table)
        record = OutboxRecord(
            event_type="Example", payload={"nested": {"count": 2}, "active": True},
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        await store.save([record])
        found = await store.fetch_unpublished()
        assert len(found) == 1
        assert found[0].id == record.id
        assert found[0].payload == record.payload
    finally:
        await adapter.disconnect()
