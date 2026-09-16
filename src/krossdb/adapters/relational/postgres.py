"""PostgreSQL adapter. DSNs should use the ``postgresql+asyncpg://`` scheme."""

from __future__ import annotations

from krossdb.adapters.relational.base import SQLAlchemyAdapter
from krossdb.core.enums import DatabaseKind
from krossdb.factory.db_factory import register_adapter


class PostgresAdapter(SQLAlchemyAdapter):
    kind = DatabaseKind.POSTGRESQL


register_adapter(DatabaseKind.POSTGRESQL, PostgresAdapter)

__all__ = ["PostgresAdapter"]
