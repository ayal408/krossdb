"""MySQL adapter. DSNs should use the ``mysql+asyncmy://`` scheme."""

from __future__ import annotations

from krossdb.adapters.relational.base import SQLAlchemyAdapter
from krossdb.core.enums import DatabaseKind
from krossdb.factory.db_factory import register_adapter


class MySQLAdapter(SQLAlchemyAdapter):
    kind = DatabaseKind.MYSQL


register_adapter(DatabaseKind.MYSQL, MySQLAdapter)

__all__ = ["MySQLAdapter"]
