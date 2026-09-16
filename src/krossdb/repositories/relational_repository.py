"""Generic repository over any SQLAlchemy Core ``Table``, backed by :class:`SQLAlchemyAdapter`.

Works identically against Postgres/MySQL/SQLite: callers hand it a Pydantic
model plus a plain :class:`sqlalchemy.Table` (no ORM mapping required), and
every query is built with SQLAlchemy Core's expression language, which
parameterizes all values automatically.

Gotcha for table authors: :class:`~krossdb.models.base.Entity` timestamps are
timezone-aware (``datetime.now(UTC)``). Declare datetime columns as
``DateTime(timezone=True)`` — a plain ``DateTime`` maps to Postgres'
``TIMESTAMP WITHOUT TIME ZONE`` and asyncpg rejects binding a tz-aware value
into it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypeVar, cast

from sqlalchemy import CursorResult, Table, delete, func, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from krossdb.adapters.relational.base import SQLAlchemyAdapter
from krossdb.core.enums import RoutingRole
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import KrossDBError, RecordNotFoundError, UnsupportedOperationError
from krossdb.interfaces.repository import (
    AbstractRepository,
    BulkResult,
    FieldFilter,
    Operator,
    Page,
    SortSpec,
    Specification,
    TId,
    TModel,
    _normalize_criteria,
)

_SelectT = TypeVar("_SelectT", bound="Select[Any]")


def _condition_for_filter(table: Table, filt: FieldFilter) -> ColumnElement[bool]:
    column = cast("ColumnElement[Any]", table.c[filt.field])
    operator = filt.operator
    value = filt.value
    # SQLAlchemy's comparison dunders are typed to return Any (they can't
    # narrow object.__eq__'s signature), so each needs an explicit cast.
    if operator is Operator.EQ:
        return cast("ColumnElement[bool]", column == value)
    if operator is Operator.NE:
        return cast("ColumnElement[bool]", column != value)
    if operator is Operator.GT:
        return cast("ColumnElement[bool]", column > value)
    if operator is Operator.GTE:
        return cast("ColumnElement[bool]", column >= value)
    if operator is Operator.LT:
        return cast("ColumnElement[bool]", column < value)
    if operator is Operator.LTE:
        return cast("ColumnElement[bool]", column <= value)
    if operator is Operator.IN:
        return column.in_(value)
    if operator is Operator.NOT_IN:
        return ~column.in_(value)
    if operator is Operator.CONTAINS:
        return column.contains(value, autoescape=True)
    if operator is Operator.IS_NULL:
        # `value` defaults to None (falsy) on FieldFilter, so treat only an
        # explicit False as "IS NOT NULL" — otherwise the operator's own name
        # would be contradicted by omitting `value` entirely.
        return column.is_not(None) if value is False else column.is_(None)
    raise UnsupportedOperationError(
        f"Operator {operator!r} is not supported by the relational adapter"
    )


def _apply_criteria(
    stmt: _SelectT, table: Table, criteria: Mapping[str, Any] | Specification | None
) -> _SelectT:
    for filt in _normalize_criteria(criteria):
        stmt = stmt.where(_condition_for_filter(table, filt))
    return stmt


class SQLAlchemyRepository(AbstractRepository[TModel, TId]):
    """CRUD/bulk/query implementation shared by Postgres, MySQL, and SQLite.

    ``id_column`` names the primary-key column used by :meth:`get_by_id`,
    :meth:`update`, :meth:`delete`, and the bulk variants.
    """

    def __init__(
        self,
        adapter: SQLAlchemyAdapter,
        table: Table,
        model: type[TModel],
        *,
        id_column: str = "id",
    ) -> None:
        self.adapter = adapter
        self.table = table
        self.model = model
        self.id_column = id_column

    def _row_to_model(self, row: RowMapping) -> TModel:
        return self.model.model_validate(dict(row))

    async def get_by_id(self, id_: TId) -> TModel | None:
        stmt = select(self.table).where(self.table.c[self.id_column] == id_)
        async with self.adapter.acquire(role=RoutingRole.REPLICA) as session:
            with translate_exceptions(table=self.table.name, op="get_by_id"):
                result = await session.execute(stmt)
            row = result.mappings().first()
        return self._row_to_model(row) if row is not None else None

    async def create(self, entity: TModel, *, idempotency_key: str | None = None) -> TModel:
        # Python mode (not "json"): keeps UUID/datetime/etc. as native Python
        # objects so they bind correctly against native column types (e.g.
        # SQLAlchemy 2.0's `Uuid`/`DateTime`) instead of being pre-stringified.
        values = entity.model_dump(mode="python")
        stmt = self.table.insert().values(**values)
        async with self.adapter.acquire(role=RoutingRole.MASTER) as session:
            with translate_exceptions(table=self.table.name, op="create"):
                await session.execute(stmt)
        return entity

    async def update(self, id_: TId, changes: Mapping[str, Any]) -> TModel:
        stmt = update(self.table).where(self.table.c[self.id_column] == id_).values(**dict(changes))
        async with self.adapter.acquire(role=RoutingRole.MASTER) as session:
            with translate_exceptions(table=self.table.name, op="update"):
                result = cast("CursorResult[Any]", await session.execute(stmt))
        if result.rowcount == 0:
            raise RecordNotFoundError(self.model.__name__, id_)
        updated = await self.get_by_id(id_)
        assert updated is not None  # we just confirmed the row exists via rowcount
        return updated

    async def delete(self, id_: TId) -> bool:
        stmt = delete(self.table).where(self.table.c[self.id_column] == id_)
        async with self.adapter.acquire(role=RoutingRole.MASTER) as session:
            with translate_exceptions(table=self.table.name, op="delete"):
                result = cast("CursorResult[Any]", await session.execute(stmt))
        return result.rowcount > 0

    async def find(
        self,
        criteria: Mapping[str, Any] | Specification | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[SortSpec] | None = None,
    ) -> Page[TModel]:
        stmt = _apply_criteria(select(self.table), self.table, criteria)
        if sort:
            for spec in sort:
                column = self.table.c[spec.field]
                stmt = stmt.order_by(column.desc() if spec.descending else column.asc())

        count_stmt = select(func.count()).select_from(stmt.subquery())

        async with self.adapter.acquire(role=RoutingRole.REPLICA) as session:
            with translate_exceptions(table=self.table.name, op="find"):
                total = (await session.execute(count_stmt)).scalar_one()
                result = await session.execute(stmt.limit(limit).offset(offset))
            items = [self._row_to_model(row) for row in result.mappings().all()]

        return Page(items=items, total=total, limit=limit, offset=offset)

    async def count(self, criteria: Mapping[str, Any] | Specification | None = None) -> int:
        stmt = _apply_criteria(select(func.count()).select_from(self.table), self.table, criteria)
        async with self.adapter.acquire(role=RoutingRole.REPLICA) as session:
            with translate_exceptions(table=self.table.name, op="count"):
                return (await session.execute(stmt)).scalar_one()

    async def bulk_create(
        self, entities: Sequence[TModel], *, idempotency_key: str | None = None
    ) -> BulkResult[TModel]:
        succeeded: list[TModel] = []
        failed: dict[int, str] = {}
        async with self.adapter.acquire(role=RoutingRole.MASTER) as session:
            for index, entity in enumerate(entities):
                try:
                    # Each insert gets its own SAVEPOINT, so one failure only
                    # discards that item instead of the whole batch (and
                    # doesn't poison the outer transaction for the remaining
                    # items or for callers running this inside a UoW).
                    async with session.begin_nested():
                        with translate_exceptions(table=self.table.name, op="bulk_create"):
                            await session.execute(
                                self.table.insert().values(**entity.model_dump(mode="python"))
                            )
                    succeeded.append(entity)
                except KrossDBError as exc:
                    failed[index] = str(exc)
        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_update(self, updates: Mapping[TId, Mapping[str, Any]]) -> BulkResult[TModel]:
        succeeded: list[TModel] = []
        failed: dict[int, str] = {}
        for index, (id_, changes) in enumerate(updates.items()):
            try:
                succeeded.append(await self.update(id_, changes))
            except KrossDBError as exc:
                failed[index] = str(exc)
        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_delete(self, ids: Sequence[TId]) -> int:
        if not ids:
            return 0
        stmt = delete(self.table).where(self.table.c[self.id_column].in_(ids))
        async with self.adapter.acquire(role=RoutingRole.MASTER) as session:
            with translate_exceptions(table=self.table.name, op="bulk_delete"):
                result = cast("CursorResult[Any]", await session.execute(stmt))
        return result.rowcount


__all__ = ["SQLAlchemyRepository"]
