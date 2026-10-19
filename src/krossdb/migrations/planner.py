"""Read-only additive schema planning using Alembic's dialect-aware comparison."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import MetaData, select
from sqlalchemy.engine import Connection
from sqlalchemy.sql.functions import FunctionElement

from krossdb.core.exceptions import ConfigurationError, UnsupportedOperationError


def _alembic() -> tuple[Any, Any, Any]:
    try:
        from alembic.autogenerate import produce_migrations
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
    except ImportError as exc:
        raise ConfigurationError("Install krossdb[migrations] for schema migrations") from exc
    return MigrationContext, Operations, produce_migrations


@dataclass(frozen=True)
class SchemaChange:
    operation: str
    table: str
    name: str
    automatic: bool
    reason: str = ""


@dataclass(frozen=True)
class SchemaPlan:
    changes: tuple[SchemaChange, ...]
    _operations: tuple[Any, ...] = field(repr=False, compare=False)

    @property
    def blocked(self) -> tuple[SchemaChange, ...]:
        return tuple(change for change in self.changes if not change.automatic)


def plan_schema(connection: Connection, metadata: MetaData) -> SchemaPlan:
    """Inspect only; never create a history table or execute DDL.

    Unmanaged tables/columns are preserved. Changes to existing definitions,
    including a rename represented as add/drop, block automatic application.
    """
    context_type, _, produce = _alembic()

    def include_object(obj: Any, name: str, type_: str, reflected: bool, compare_to: Any) -> bool:
        return not (type_ == "table" and reflected and compare_to is None)

    context = context_type.configure(connection, opts={
        "include_object": include_object,
        "compare_type": True,
        "compare_server_default": True,
    })
    script = produce(context, metadata)
    changes: list[SchemaChange] = []
    operations: list[Any] = []

    def visit(op: Any) -> None:
        if hasattr(op, "ops"):
            for child in op.ops:
                visit(child)
            return
        kind = type(op).__name__
        table = str(getattr(op, "table_name", None) or getattr(op, "source_table", ""))
        name = str(getattr(op, "column_name", None) or getattr(op, "constraint_name", None)
                   or getattr(op, "index_name", None) or table)
        allowed = kind in {
            "CreateTableOp", "AddColumnOp", "CreateIndexOp",
            "CreateForeignKeyOp", "CreateUniqueConstraintOp",
        }
        reason = "" if allowed else "Existing definitions require an explicit migration"
        if kind == "AddColumnOp":
            column = op.column
            name = str(column.name)
            if column.primary_key:
                allowed, reason = False, "Adding a primary key requires an explicit migration"
            elif not column.nullable and column.server_default is None:
                schema = getattr(op, "schema", None)
                target = metadata.tables[f"{schema}.{table}" if schema else table]
                # Select a literal: new columns in target metadata do not exist yet.
                populated = connection.execute(select(1).select_from(target).limit(1)).first()
                if populated:
                    allowed, reason = False, "Backfill existing rows before adding a required column"
            if (connection.dialect.name == "sqlite" and column.server_default is not None
                    and isinstance(column.server_default.arg, FunctionElement)):
                allowed, reason = False, "SQLite function defaults on added columns require an explicit migration"
        if connection.dialect.name == "sqlite" and kind in {
            "CreateForeignKeyOp", "CreateUniqueConstraintOp",
        }:
            allowed, reason = False, "SQLite constraint changes require an explicit Alembic batch migration"
        changes.append(SchemaChange(kind, table, name, allowed, reason))
        operations.append(op)

    visit(script.upgrade_ops)
    return SchemaPlan(tuple(changes), tuple(operations))


def apply_schema(connection: Connection, metadata: MetaData) -> SchemaPlan:
    """Replan on this connection and apply additive operations in its transaction.

    The caller owns the transaction and migration lock. Use MigrationRunner
    for the complete atomic/serialized flow. No operations run if any are blocked.
    """
    context_type, operations_type, _ = _alembic()
    plan = plan_schema(connection, metadata)
    if plan.blocked:
        details = "; ".join(f"{c.table}.{c.name}: {c.reason}" for c in plan.blocked)
        raise UnsupportedOperationError(f"Schema migration requires explicit changes: {details}")
    operations = operations_type(context_type.configure(connection))
    for op in plan._operations:
        operations.invoke(op)
    return plan


__all__ = ["SchemaChange", "SchemaPlan", "apply_schema", "plan_schema"]
