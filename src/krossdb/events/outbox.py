"""Outbox store implementations for the transactional outbox pattern.

:class:`InMemoryOutboxStore` is for tests/dev. :class:`SQLAlchemyOutboxStore`
persists records to a caller-provided SQLAlchemy Core ``Table``; used from
inside a :class:`~krossdb.uow.sqlalchemy_uow.SQLAlchemyUnitOfWork`, its writes
go through the same pinned session as the business data, so the event record
and the business change commit or roll back atomically. Both are drained by
:class:`~krossdb.events.relay.OutboxRelay`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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

    async def fetch_unpublished(
        self, *, limit: int = 100, max_attempts: int | None = None
    ) -> list[OutboxRecord]:
        _validate_fetch_args(limit, max_attempts)
        unpublished = [
            r
            for r in self._records.values()
            if r.published_at is None and (max_attempts is None or r.attempts < max_attempts)
        ]
        return unpublished[:limit]

    async def mark_published(self, record_ids: list[uuid.UUID]) -> None:
        now = datetime.now(UTC)
        for record_id in record_ids:
            record = self._records.get(record_id)
            if record is not None:
                self._records[record_id] = record.model_copy(update={"published_at": now})

    async def record_failed_attempts(self, record_ids: list[uuid.UUID]) -> None:
        for record_id in record_ids:
            record = self._records.get(record_id)
            if record is not None:
                self._records[record_id] = record.model_copy(update={"attempts": record.attempts + 1})


class SQLAlchemyOutboxStore(AbstractOutboxStore):
    """Persists outbox records to a SQLAlchemy Core table.

    Expected columns: ``id`` (str), ``event_type`` (str), ``payload`` (text/JSON),
    ``created_at`` (datetime), ``published_at`` (datetime, nullable), ``attempts`` (int).

    :meth:`fetch_unpublished` selects ``FOR UPDATE SKIP LOCKED`` (on dialects
    that support it, e.g. PostgreSQL and MySQL 8+; SQLite has no row locks
    and ignores it). Inside :meth:`claim` those locks are held until the
    batch commits, so concurrent relays each get a disjoint set of records.
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
                # The adapter owns commits outside a UoW; inside a UoW
                # committing here would prematurely commit business changes.

    @asynccontextmanager
    async def claim(self) -> AsyncIterator[None]:
        # One master transaction for the whole batch: every acquire() inside
        # reuses it, so row locks taken by fetch_unpublished() survive until
        # the records are marked, and are released by the commit/rollback.
        # A UoW binds the session without itself going through acquire(), so
        # each store call takes exactly one circuit-breaker slot.
        # Local import: krossdb.uow.sqlalchemy_uow imports this module.
        from krossdb.uow.sqlalchemy_uow import SQLAlchemyUnitOfWork

        async with SQLAlchemyUnitOfWork(self.adapter) as uow:
            yield
            await uow.commit()

    async def fetch_unpublished(
        self, *, limit: int = 100, max_attempts: int | None = None
    ) -> list[OutboxRecord]:
        _validate_fetch_args(limit, max_attempts)
        stmt = select(self.table).where(self.table.c.published_at.is_(None))
        if max_attempts is not None:
            stmt = stmt.where(self.table.c.attempts < max_attempts)
        stmt = (
            stmt.order_by(self.table.c.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
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
                # The adapter owns commits outside a UoW; inside a UoW
                # committing here would prematurely commit business changes.

    async def record_failed_attempts(self, record_ids: list[uuid.UUID]) -> None:
        if not record_ids:
            return
        stmt = (
            update(self.table)
            .where(self.table.c.id.in_([str(r) for r in record_ids]))
            .values(attempts=self.table.c.attempts + 1)
        )
        async with self.adapter.acquire() as session:
            with translate_exceptions(table=self.table.name, op="outbox_record_failed_attempts"):
                await session.execute(stmt)


def _validate_fetch_args(limit: int, max_attempts: int | None) -> None:
    if limit <= 0:
        raise ValueError("limit must be positive")
    if max_attempts is not None and max_attempts <= 0:
        raise ValueError("max_attempts must be positive")


__all__ = ["InMemoryOutboxStore", "SQLAlchemyOutboxStore"]
