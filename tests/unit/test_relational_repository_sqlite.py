"""Functional tests for the relational stack against real (in-memory) SQLite.

Unlike the mocked-adapter unit tests elsewhere in this package, these exercise
the real :mod:`sqlalchemy` async engine end-to-end (real SQL, real constraint
violations, real transactions) without requiring Docker, since SQLite runs
in-process. This is what proves :class:`SQLAlchemyRepository`,
:class:`SQLAlchemyUnitOfWork`, and the exception mapper actually work
together correctly, complementing the mocked tests (which check call shape)
and the Testcontainers integration tests (which check real Postgres/Mongo/
Qdrant wire behavior).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Uuid

from krossdb.adapters.relational.sqlite import SQLiteAdapter
from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import DatabaseKind, RoutingRole
from krossdb.core.exceptions import (
    DuplicateRecordError,
    RecordNotFoundError,
    UnsupportedOperationError,
)
from krossdb.interfaces.repository import FieldFilter, Operator
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
    # timezone=True: Entity.created_at/updated_at are tz-aware (datetime.now(UTC)).
    Column("created_at", DateTime(timezone=True)),
    Column("updated_at", DateTime(timezone=True)),
    Column("version", Integer),
    Column("name", String, unique=True),
    Column("quantity", Integer),
)


class Widget(Entity):
    name: str
    quantity: int = 0


@pytest_asyncio.fixture
async def sqlite_adapter() -> AsyncIterator[SQLiteAdapter]:
    config = ConnectionConfig(
        name="test",
        kind=DatabaseKind.SQLITE,
        nodes=[NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
    )
    adapter = SQLiteAdapter(config)
    await adapter.connect()
    async with adapter.acquire() as session:
        await session.run_sync(lambda sync_session: _metadata.create_all(sync_session.connection()))
    yield adapter
    await adapter.disconnect()


@pytest.fixture
def widget_sql_repo(sqlite_adapter: SQLiteAdapter) -> SQLAlchemyRepository[Widget, uuid.UUID]:
    return SQLAlchemyRepository(sqlite_adapter, _widgets_table, Widget)


async def test_create_and_get_round_trip(widget_sql_repo: SQLAlchemyRepository) -> None:
    widget = Widget(name="alpha", quantity=1)

    await widget_sql_repo.create(widget)
    fetched = await widget_sql_repo.get_by_id(widget.id)

    assert fetched is not None
    assert fetched.id == widget.id
    assert fetched.name == "alpha"


async def test_unique_constraint_violation_maps_to_duplicate_record_error(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    await widget_sql_repo.create(Widget(name="dup", quantity=1))

    with pytest.raises(DuplicateRecordError):
        await widget_sql_repo.create(Widget(name="dup", quantity=2))


async def test_update_missing_row_raises_record_not_found(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    with pytest.raises(RecordNotFoundError):
        await widget_sql_repo.update(uuid.uuid4(), {"quantity": 5})


async def test_update_persists_changes(widget_sql_repo: SQLAlchemyRepository) -> None:
    widget = await widget_sql_repo.create(Widget(name="before", quantity=1))

    updated = await widget_sql_repo.update(widget.id, {"quantity": 42})

    assert updated.quantity == 42
    refetched = await widget_sql_repo.get_by_id(widget.id)
    assert refetched.quantity == 42


async def test_delete_is_committed_immediately_outside_a_uow(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    widget = await widget_sql_repo.create(Widget(name="deleteme", quantity=1))

    assert await widget_sql_repo.delete(widget.id) is True
    assert await widget_sql_repo.get_by_id(widget.id) is None


async def test_bulk_create_reports_partial_success_and_keeps_successful_rows(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    w1 = Widget(name="dup2", quantity=1)
    w2 = Widget(name="dup2", quantity=2)  # collides with w1 on the unique name column
    w3 = Widget(name="unique2", quantity=3)

    result = await widget_sql_repo.bulk_create([w1, w2, w3])

    assert result.success_count == 2
    assert result.failure_count == 1
    # The savepoint-per-item strategy must not roll back the whole batch:
    assert await widget_sql_repo.get_by_id(w1.id) is not None
    assert await widget_sql_repo.get_by_id(w3.id) is not None


async def test_bulk_delete_returns_count_removed(widget_sql_repo: SQLAlchemyRepository) -> None:
    widgets = [Widget(name=f"bulk-{i}", quantity=i) for i in range(3)]
    await widget_sql_repo.bulk_create(widgets)

    deleted = await widget_sql_repo.bulk_delete([w.id for w in widgets])

    assert deleted == 3


# --------------------------------------------------------------------------
# extra_criteria: proves the tenant-scoping fix is atomic at the real-SQL
# level (a single `WHERE id = ? AND tenant_id = ?`), not a separate read
# followed by an unscoped write. See krossdb.multitenancy.context and
# issue #35 (TenantScopedRepository.update()/delete() TOCTOU).
# --------------------------------------------------------------------------


async def test_update_with_matching_extra_criteria_succeeds(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    widget = Widget(name="scoped-owned", quantity=1, tenant_id="tenant-a")
    await widget_sql_repo.create(widget)

    updated = await widget_sql_repo.update(
        widget.id,
        {"quantity": 99},
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-a")],
    )

    assert updated.quantity == 99


async def test_update_with_non_matching_extra_criteria_raises_record_not_found(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    """A real UPDATE ... WHERE id = ? AND tenant_id = ? against SQLite:
    the row exists (under a different tenant) but the query's own WHERE
    clause excludes it, so rowcount is 0 — proving the scoping happens in
    the query itself, not in a separate check before it."""
    widget = Widget(name="scoped-foreign", quantity=1, tenant_id="tenant-a")
    await widget_sql_repo.create(widget)

    with pytest.raises(RecordNotFoundError):
        await widget_sql_repo.update(
            widget.id,
            {"quantity": 99},
            extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-b")],
        )

    # the row must be completely untouched
    untouched = await widget_sql_repo.get_by_id(widget.id)
    assert untouched.quantity == 1
    assert untouched.tenant_id == "tenant-a"


async def test_delete_with_non_matching_extra_criteria_leaves_row_intact(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    widget = Widget(name="scoped-delete", quantity=1, tenant_id="tenant-a")
    await widget_sql_repo.create(widget)

    deleted = await widget_sql_repo.delete(
        widget.id,
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-b")],
    )

    assert deleted is False
    assert await widget_sql_repo.get_by_id(widget.id) is not None


async def test_bulk_update_with_extra_criteria_reports_non_matching_ids_as_failures(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    owned = Widget(name="bulk-scoped-owned", quantity=1, tenant_id="tenant-a")
    foreign = Widget(name="bulk-scoped-foreign", quantity=1, tenant_id="tenant-b")
    await widget_sql_repo.bulk_create([owned, foreign])

    result = await widget_sql_repo.bulk_update(
        {owned.id: {"quantity": 2}, foreign.id: {"quantity": 2}},
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-a")],
    )

    assert [w.id for w in result.succeeded] == [owned.id]
    assert len(result.failed) == 1
    untouched = await widget_sql_repo.get_by_id(foreign.id)
    assert untouched.quantity == 1


async def test_bulk_delete_with_extra_criteria_only_removes_matching_rows(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    owned = Widget(name="bulk-del-owned", quantity=1, tenant_id="tenant-a")
    foreign = Widget(name="bulk-del-foreign", quantity=1, tenant_id="tenant-b")
    await widget_sql_repo.bulk_create([owned, foreign])

    deleted = await widget_sql_repo.bulk_delete(
        [owned.id, foreign.id],
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-a")],
    )

    assert deleted == 1
    assert await widget_sql_repo.get_by_id(owned.id) is None
    assert await widget_sql_repo.get_by_id(foreign.id) is not None


async def test_find_with_criteria_and_pagination(widget_sql_repo: SQLAlchemyRepository) -> None:
    for i in range(5):
        await widget_sql_repo.create(Widget(name=f"page-{i}", quantity=1))

    page = await widget_sql_repo.find({"quantity": 1}, limit=2, offset=1)

    assert page.total == 5
    assert len(page.items) == 2
    assert page.has_more is True


async def test_find_with_plain_dict_criteria_is_still_supported(
    widget_sql_repo: SQLAlchemyRepository,
) -> None:
    await widget_sql_repo.create(Widget(name="dict-alpha", quantity=1))
    await widget_sql_repo.create(Widget(name="dict-beta", quantity=2))

    page = await widget_sql_repo.find({"quantity": 2})

    assert page.total == 1
    assert page.items[0].name == "dict-beta"


async def test_find_with_ne_operator(widget_sql_repo: SQLAlchemyRepository) -> None:
    await widget_sql_repo.create(Widget(name="ne-a", quantity=1))
    await widget_sql_repo.create(Widget(name="ne-b", quantity=2))

    page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.NE, value=1)]
    )

    assert page.total == 1
    assert page.items[0].name == "ne-b"


async def test_find_with_gt_and_lte_operators(widget_sql_repo: SQLAlchemyRepository) -> None:
    for i in range(5):
        await widget_sql_repo.create(Widget(name=f"range-{i}", quantity=i))

    gt_page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.GT, value=2)]
    )
    lte_page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.LTE, value=2)]
    )

    assert {w.quantity for w in gt_page.items} == {3, 4}
    assert {w.quantity for w in lte_page.items} == {0, 1, 2}


async def test_find_with_gte_and_lt_operators(widget_sql_repo: SQLAlchemyRepository) -> None:
    for i in range(5):
        await widget_sql_repo.create(Widget(name=f"range2-{i}", quantity=i))

    gte_page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.GTE, value=3)]
    )
    lt_page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.LT, value=1)]
    )

    assert {w.quantity for w in gte_page.items} == {3, 4}
    assert {w.quantity for w in lt_page.items} == {0}


async def test_find_with_in_and_not_in_operators(widget_sql_repo: SQLAlchemyRepository) -> None:
    for i in range(4):
        await widget_sql_repo.create(Widget(name=f"in-{i}", quantity=i))

    in_page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.IN, value=[0, 2])]
    )
    not_in_page = await widget_sql_repo.find(
        [FieldFilter(field="quantity", operator=Operator.NOT_IN, value=[0, 2])]
    )

    assert {w.quantity for w in in_page.items} == {0, 2}
    assert {w.quantity for w in not_in_page.items} == {1, 3}


async def test_find_with_contains_operator(widget_sql_repo: SQLAlchemyRepository) -> None:
    await widget_sql_repo.create(Widget(name="banana-split", quantity=1))
    await widget_sql_repo.create(Widget(name="apple-pie", quantity=1))

    page = await widget_sql_repo.find(
        [FieldFilter(field="name", operator=Operator.CONTAINS, value="split")]
    )

    assert page.total == 1
    assert page.items[0].name == "banana-split"


async def test_find_with_is_null_operator(widget_sql_repo: SQLAlchemyRepository) -> None:
    await widget_sql_repo.create(Widget(name="tenant-none", quantity=1, tenant_id=None))
    await widget_sql_repo.create(Widget(name="tenant-set", quantity=1, tenant_id="acme"))

    null_page = await widget_sql_repo.find(
        [FieldFilter(field="tenant_id", operator=Operator.IS_NULL, value=True)]
    )
    not_null_page = await widget_sql_repo.find(
        [FieldFilter(field="tenant_id", operator=Operator.IS_NULL, value=False)]
    )

    assert null_page.total == 1
    assert null_page.items[0].name == "tenant-none"
    assert not_null_page.total == 1
    assert not_null_page.items[0].name == "tenant-set"


async def test_count_with_operator_filters(widget_sql_repo: SQLAlchemyRepository) -> None:
    for i in range(3):
        await widget_sql_repo.create(Widget(name=f"count-{i}", quantity=i))

    total = await widget_sql_repo.count(
        [FieldFilter(field="quantity", operator=Operator.GTE, value=1)]
    )

    assert total == 2


async def test_exists_with_operator_filters(widget_sql_repo: SQLAlchemyRepository) -> None:
    await widget_sql_repo.create(Widget(name="exists-check", quantity=5))

    assert await widget_sql_repo.exists(
        [FieldFilter(field="quantity", operator=Operator.GT, value=4)]
    )
    assert not await widget_sql_repo.exists(
        [FieldFilter(field="quantity", operator=Operator.GT, value=100)]
    )


async def test_find_raises_for_unsupported_operator(widget_sql_repo: SQLAlchemyRepository) -> None:
    bogus_filter = FieldFilter.model_construct(field="quantity", operator="bogus", value=1)

    with pytest.raises(UnsupportedOperationError):
        await widget_sql_repo.find([bogus_filter])


async def test_uow_commit_persists_changes(sqlite_adapter: SQLiteAdapter, widget_sql_repo) -> None:
    widget = Widget(name="committed", quantity=1)

    async with SQLAlchemyUnitOfWork(sqlite_adapter) as uow:
        await widget_sql_repo.create(widget)
        await uow.commit()

    assert await widget_sql_repo.get_by_id(widget.id) is not None


async def test_uow_rollback_discards_changes(
    sqlite_adapter: SQLiteAdapter, widget_sql_repo
) -> None:
    widget = Widget(name="rolledback", quantity=1)

    async with SQLAlchemyUnitOfWork(sqlite_adapter) as uow:
        await widget_sql_repo.create(widget)
        await uow.rollback()

    assert await widget_sql_repo.get_by_id(widget.id) is None


async def test_uow_implicit_rollback_on_exception(
    sqlite_adapter: SQLiteAdapter, widget_sql_repo
) -> None:
    widget = Widget(name="exploded", quantity=1)

    with pytest.raises(ValueError):
        async with SQLAlchemyUnitOfWork(sqlite_adapter):
            await widget_sql_repo.create(widget)
            raise ValueError("business rule failed after the write")

    assert await widget_sql_repo.get_by_id(widget.id) is None


async def test_uow_spans_multiple_repository_calls_atomically(
    sqlite_adapter: SQLiteAdapter, widget_sql_repo
) -> None:
    w1 = Widget(name="atomic-1", quantity=1)
    w2 = Widget(name="atomic-2", quantity=2)

    async with SQLAlchemyUnitOfWork(sqlite_adapter) as uow:
        await widget_sql_repo.create(w1)
        await widget_sql_repo.create(w2)
        await uow.commit()

    assert await widget_sql_repo.get_by_id(w1.id) is not None
    assert await widget_sql_repo.get_by_id(w2.id) is not None


async def test_health_check_reports_healthy_when_connected(sqlite_adapter: SQLiteAdapter) -> None:
    result = await sqlite_adapter.health_check()

    assert str(result.status) == "healthy"
    assert result.latency_ms >= 0
