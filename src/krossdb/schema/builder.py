"""Turn a :class:`~krossdb.schema.config.SchemaConfig` into a real ``sqlalchemy.MetaData``.

SQLAlchemy's own ``MetaData.create_all`` already topologically sorts tables by their
foreign keys and skips anything that already exists, so the builder only needs to produce
correct ``Table``/``Column``/``ForeignKeyConstraint``/``Index`` objects — no manual
dependency ordering or existence checks, unlike amandex's hand-rolled migration runner.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    Uuid,
    func,
)
from sqlalchemy.types import TypeEngine

from krossdb.schema.config import ColumnSpec, ColumnType, SchemaConfig, TableSpec

_DEFAULT_STRING_LENGTH = 255


def _sqlalchemy_type(spec: ColumnSpec) -> TypeEngine[Any]:
    if spec.type is ColumnType.STRING:
        return String(length=spec.length or _DEFAULT_STRING_LENGTH)
    if spec.type is ColumnType.TEXT:
        return Text()
    if spec.type is ColumnType.INTEGER:
        return Integer()
    if spec.type is ColumnType.BIGINT:
        from sqlalchemy import BigInteger

        return BigInteger()
    if spec.type is ColumnType.FLOAT:
        return Float()
    if spec.type is ColumnType.NUMERIC:
        return Numeric(precision=spec.precision, scale=spec.scale)
    if spec.type is ColumnType.BOOLEAN:
        return Boolean()
    if spec.type is ColumnType.DATE:
        return Date()
    if spec.type is ColumnType.DATETIME:
        # Always timezone-aware: krossdb's own Entity model uses tz-aware timestamps
        # (see models/base.py), and a naive DateTime silently rejects them on Postgres.
        return DateTime(timezone=True)
    if spec.type is ColumnType.UUID:
        return Uuid()
    if spec.type is ColumnType.JSON:
        from sqlalchemy import JSON

        return JSON()
    raise AssertionError(f"unhandled ColumnType: {spec.type!r}")  # pragma: no cover


def _server_default(spec: ColumnSpec) -> Any:
    if spec.default is None:
        return None
    if spec.type is ColumnType.DATETIME and spec.default == "now":
        return func.now()
    return str(spec.default)


def _build_column(spec: ColumnSpec) -> Column[Any]:
    return Column(
        spec.name,
        _sqlalchemy_type(spec),
        primary_key=spec.primary_key,
        nullable=not spec.required and not spec.primary_key,
        unique=spec.unique or None,  # False would emit a pointless UNIQUE()-less constraint
        server_default=_server_default(spec),
    )


def _build_table(spec: TableSpec, metadata: MetaData) -> Table:
    columns = [_build_column(c) for c in spec.columns]

    constraints: list[ForeignKeyConstraint] = [
        ForeignKeyConstraint(
            fk.columns,
            [f"{fk.ref_table}.{ref_col}" for ref_col in fk.ref_columns],
            ondelete=fk.on_delete,
        )
        for fk in spec.foreign_keys
    ]

    indexes: list[Index] = [
        Index(idx.name or f"ix_{spec.name}_{'_'.join(idx.columns)}", *idx.columns, unique=idx.unique)
        for idx in spec.indexes
    ]

    return Table(spec.name, metadata, *columns, *constraints, *indexes)


def build_metadata(schema: SchemaConfig, *, metadata: MetaData | None = None) -> MetaData:
    """Build (or extend) a ``sqlalchemy.MetaData`` from a validated :class:`SchemaConfig`.

    Pass an existing ``metadata`` to merge a declarative schema alongside tables already
    defined in code; otherwise a fresh one is created and returned.
    """

    metadata = metadata if metadata is not None else MetaData()
    for table_spec in schema.tables:
        _build_table(table_spec, metadata)
    return metadata


__all__ = ["build_metadata"]
