"""Outbox store implementations for the transactional outbox pattern.

:class:`InMemoryOutboxStore` is for tests/dev. :class:`SQLAlchemyOutboxStore`
persists records to a caller-provided SQLAlchemy Core ``Table``; used from
inside a :class:`~krossdb.uow.sqlalchemy_uow.SQLAlchemyUnitOfWork`, its writes
go through the same pinned session as the business data, so the event record
and the business change commit or roll back atomically.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

from sqlalchemy import Table, select, update

from krossdb.adapters.relational.base import SQLAlchemyAdapter
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.interfaces.events import AbstractOutboxStore, OutboxRecord


class InMemoryOutboxStore(AbstractOutboxStore):
    def __init__(self) -> None:
        self._records: dict[uuid.UUID, OutboxRecord] = {}

    async def save(self, records: list[OutboxRecord]) -> None:
        for record in records:
            self._records[record.id] = record

    async def fetch_unpublished(self, *, limit: int = 100) -> list[OutboxRecord]:
        unpublished = [r for r in self._records.values() if r.published_at is None]
        return unpublished[:limit]

    async def mark_published(self, record_ids: list[uuid.UUID]) -> None:
        now = datetime.now(UTC)
        for record_id in record_ids:
            record = self._records.get(record_id)
            if record is not None:
                self._records[record_id] = record.model_copy(update={"published_at": now})


class SQLAlchemyOutboxStore(AbstractOutboxStore):
    """Persists outbox records to a SQLAlchemy Core table.

    Expected columns: ``id`` (str), ``event_type`` (str), ``payload`` (text/JSON),
    ``created_at`` (datetime), ``published_at`` (datetime, nullable), ``attempts`` (int).
    """

    def __init__(self, adapter: SQLAlchemyAdapter, table: Table) -> None:
        self.adapter = adapter
        self.table = table

    async def save(self, records: list[OutboxRecord]) -> None:
        if not records:
            return
        async with self.adapter.acquire() as session:
            with translate_exceptions(table=self.table.name, op="outbox_save"):
                for record in records:
                    await session.execute(
                        self.table.insert().values(
                            id=str(record.id),
                            event_type=record.event_type,
                            payload=json.dumps(record.payload),
                            created_at=record.created_at,
                            published_at=record.published_at,
                            attempts=record.attempts,
                        )
                    )
                await session.commit()

    async def fetch_unpublished(self, *, limit: int = 100) -> list[OutboxRecord]:
        stmt = (
            select(self.table)
            .where(self.table.c.published_at.is_(None))
            .order_by(self.table.c.created_at.asc())
            .limit(limit)
        )
        async with self.adapter.acquire() as session:
            with translate_exceptions(table=self.table.name, op="outbox_fetch"):
                result = await session.execute(stmt)
            rows = result.mappings().all()

        return [
            OutboxRecord(
                id=uuid.UUID(str(row["id"])),
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                created_at=row["created_at"],
                published_at=row["published_at"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    async def mark_published(self, record_ids: list[uuid.UUID]) -> None:
        if not record_ids:
            return
        stmt = (
            update(self.table)
            .where(self.table.c.id.in_([str(r) for r in record_ids]))
            .values(published_at=datetime.now(UTC))
        )
        async with self.adapter.acquire() as session:
            with translate_exceptions(table=self.table.name, op="outbox_mark_published"):
                await session.execute(stmt)
                await session.commit()


__all__ = ["InMemoryOutboxStore", "SQLAlchemyOutboxStore"]
