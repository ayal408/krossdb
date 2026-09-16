"""Declarative schema definitions: describe tables in YAML/JSON instead of SQLAlchemy Core code.

Inspired by amandex's ``db_config.json`` table-definition format, adapted for krossdb's
async, multi-dialect (Postgres/MySQL/SQLite) adapters: portable column types, foreign keys,
and indexes, loaded from either YAML or JSON and turned into a ``sqlalchemy.MetaData``
that :func:`krossdb.cli.commands.schema` (or any caller) can hand to ``create_all``.
"""

from __future__ import annotations

from krossdb.schema.builder import build_metadata
from krossdb.schema.config import (
    ColumnSpec,
    ColumnType,
    ForeignKeySpec,
    IndexSpec,
    SchemaConfig,
    TableSpec,
    load_schema_config,
)

__all__ = [
    "ColumnSpec",
    "ColumnType",
    "ForeignKeySpec",
    "IndexSpec",
    "SchemaConfig",
    "TableSpec",
    "build_metadata",
    "load_schema_config",
]
