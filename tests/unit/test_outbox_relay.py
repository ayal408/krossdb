"""OutboxRelay plus the store features it relies on (attempt tracking, claiming)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, select
from sqlalchemy.dialects import mysql, postgresql

from krossdb.adapters.relational.sqlite import SQLiteAdapter
from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import DatabaseKind, RoutingRole
from krossdb.core.exceptions import QueryError
from krossdb.events.outbox import InMemoryOutboxStore, SQLAlchemyOutboxStore
from krossdb.events.relay import OutboxRelay
from krossdb.interfaces.events import AbstractOutboxStore, OutboxRecord

pytestmark = pytest.mark.unit

_metadata = MetaData()
_outbox_table = Table(
    "outbox",
    _metadata,
    Column("id", String, primary_key=True),
    Column("event_type", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False),
)


def _records(count: int) -> list[OutboxRecord]:
    start = datetime.now(UTC)
    return [
        OutboxRecord(event_type="WidgetCreated", payload={"i": i}, created_at=start + timedelta(seconds=i))
        for i in range(count)
    ]


class Collector:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.published: list[int] = []
        self._fail_on = fail_on or set()

    async def __call__(self, record: OutboxRecord) -> None:
        if record.payload["i"] in self._fail_on:
            raise RuntimeError("broker unavailable")
        self.published.append(record.payload["i"])


@pytest_asyncio.fixture
async def sql_store() -> AsyncIterator[SQLAlchemyOutboxStore]:
    adapter = SQLiteAdapter(
        ConnectionConfig(
            name="outbox",
            kind=DatabaseKind.SQLITE,
            nodes=[NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
        )
    )
    await adapter.connect()
    async with adapter.acquire() as session:
        await session.run_sync(lambda sync_session: _metadata.create_all(sync_session.connection()))
    yield SQLAlchemyOutboxStore(adapter, _outbox_table)
    await adapter.disconnect()


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest) -> AbstractOutboxStore:
    if request.param == "memory":
        return InMemoryOutboxStore()
    return request.getfixturevalue("sql_store")


async def test_relay_publishes_in_order_and_marks_records_published(store: AbstractOutboxStore) -> None:
    await store.save(_records(3))
    collector = Collector()

    published = await OutboxRelay(store, collector).run_once()

    assert published == 3
    assert collector.published == [0, 1, 2]
    assert await store.fetch_unpublished() == []


async def test_failed_publish_counts_an_attempt_and_is_retried_later(store: AbstractOutboxStore) -> None:
    await store.save(_records(3))
    relay = OutboxRelay(store, Collector(fail_on={1}))

    assert await relay.run_once() == 2

    [remaining] = await store.fetch_unpublished()
    assert remaining.payload == {"i": 1}
    assert remaining.attempts == 1


async def test_records_stop_being_fetched_after_max_attempts(store: AbstractOutboxStore) -> None:
    await store.save(_records(1))
    relay = OutboxRelay(store, Collector(fail_on={0}), max_attempts=2)

    await relay.run_once()
    await relay.run_once()
    await relay.run_once()

    assert await store.fetch_unpublished(max_attempts=2) == []
    [dead] = await store.fetch_unpublished()
    assert dead.attempts == 2


async def test_relay_respects_batch_size(store: AbstractOutboxStore) -> None:
    await store.save(_records(5))
    collector = Collector()

    assert await OutboxRelay(store, collector, batch_size=2).run_once() == 2
    assert collector.published == [0, 1]


async def test_failure_while_marking_rolls_back_the_sql_batch(
    sql_store: SQLAlchemyOutboxStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await sql_store.save(_records(2))

    async def broken_mark(record_ids: list[uuid.UUID]) -> None:
        async with sql_store.adapter.acquire() as session:
            await session.execute(_outbox_table.update().values(published_at=datetime.now(UTC)))
        raise RuntimeError("lost connection mid-batch")

    monkeypatch.setattr(sql_store, "mark_published", broken_mark)
    with pytest.raises(RuntimeError, match="lost connection"):
        await OutboxRelay(sql_store, Collector()).run_once()

    # Nothing was committed, so both records are delivered again next time.
    assert len(await sql_store.fetch_unpublished()) == 2


async def test_sql_fetch_locks_rows_and_skips_locked_ones() -> None:
    store = SQLAlchemyOutboxStore(None, _outbox_table)  # type: ignore[arg-type]
    captured: list[object] = []

    class _Session:
        async def execute(self, stmt: object) -> object:
            captured.append(stmt)
            raise RuntimeError("stop")

    class _Adapter:
        def acquire(self, **_: object):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def _cm():
                yield _Session()

            return _cm()

    store.adapter = _Adapter()  # type: ignore[assignment]
    with pytest.raises(QueryError):
        await store.fetch_unpublished(limit=5, max_attempts=3)

    [stmt] = captured
    for dialect in (postgresql.dialect(), mysql.dialect()):
        sql = str(stmt.compile(dialect=dialect))  # type: ignore[attr-defined]
        assert "FOR UPDATE SKIP LOCKED" in sql
        assert "attempts <" in sql


async def test_sql_record_failed_attempts_increments_in_place(sql_store: SQLAlchemyOutboxStore) -> None:
    [record] = _records(1)
    await sql_store.save([record])

    await sql_store.record_failed_attempts([record.id])
    await sql_store.record_failed_attempts([record.id])

    async with sql_store.adapter.acquire() as session:
        row = (await session.execute(select(_outbox_table))).mappings().one()
    assert row["attempts"] == 2
    assert json.loads(row["payload"]) == {"i": 0}


async def test_run_drains_backlog_then_stops_when_asked() -> None:
    store = InMemoryOutboxStore()
    await store.save(_records(5))
    collector = Collector()
    relay = OutboxRelay(store, collector, batch_size=2)
    stop = asyncio.Event()

    task = asyncio.create_task(relay.run(poll_interval_seconds=0.01, stop=stop))
    for _ in range(100):
        if len(collector.published) == 5:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert collector.published == [0, 1, 2, 3, 4]


async def test_run_survives_a_failing_batch() -> None:
    store = InMemoryOutboxStore()
    await store.save(_records(1))
    collector = Collector()
    calls = 0
    original_fetch = store.fetch_unpublished

    async def flaky_fetch(**kwargs: object) -> list[OutboxRecord]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unreachable")
        return await original_fetch(**kwargs)  # type: ignore[arg-type]

    store.fetch_unpublished = flaky_fetch  # type: ignore[method-assign]
    stop = asyncio.Event()
    task = asyncio.create_task(OutboxRelay(store, collector).run(poll_interval_seconds=0.01, stop=stop))
    for _ in range(100):
        if collector.published:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=1)

    assert collector.published == [0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"batch_size": 0}, "batch_size"),
        ({"max_attempts": 0}, "max_attempts"),
    ],
)
def test_relay_rejects_invalid_settings(kwargs: dict[str, int], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        OutboxRelay(InMemoryOutboxStore(), Collector(), **kwargs)


async def test_store_without_attempt_tracking_says_so() -> None:
    class MinimalStore(AbstractOutboxStore):
        async def save(self, records: list[OutboxRecord]) -> None: ...

        async def fetch_unpublished(
            self, *, limit: int = 100, max_attempts: int | None = None
        ) -> list[OutboxRecord]:
            return []

        async def mark_published(self, record_ids: list[uuid.UUID]) -> None: ...

    with pytest.raises(NotImplementedError, match="MinimalStore"):
        await MinimalStore().record_failed_attempts([uuid.uuid4()])


async def test_sql_relay_batch_can_be_the_half_open_probe() -> None:
    """claim() + the store calls inside it must not compete for one probe slot."""

    from krossdb.core.config import CircuitBreakerConfig
    from krossdb.core.enums import CircuitState
    from krossdb.core.exceptions import ConnectionError_

    adapter = SQLiteAdapter(
        ConnectionConfig(
            name="outbox-probe",
            kind=DatabaseKind.SQLITE,
            nodes=[NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
            circuit_breaker=CircuitBreakerConfig(
                failure_threshold=1, recovery_timeout_seconds=0.01, half_open_max_calls=1
            ),
        )
    )
    await adapter.connect()
    try:
        async with adapter.acquire() as session:
            await session.run_sync(lambda s: _metadata.create_all(s.connection()))
        store = SQLAlchemyOutboxStore(adapter, _outbox_table)
        await store.save(_records(1))
        breaker = adapter._breakers[adapter._select_key(RoutingRole.MASTER)]
        with pytest.raises(ConnectionError_):
            async with adapter.acquire():
                raise ConnectionError_("blip")
        await asyncio.sleep(0.02)

        assert await OutboxRelay(store, Collector()).run_once() == 1
        assert breaker.state is CircuitState.CLOSED
    finally:
        await adapter.disconnect()
