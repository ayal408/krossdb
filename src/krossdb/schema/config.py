"""Pydantic models for a declarative table schema, plus a YAML/JSON loader.

The format is deliberately small — table name, columns, foreign keys, indexes — and every
field maps onto a portable SQLAlchemy construct so the same file produces correct DDL on
Postgres, MySQL, and SQLite alike.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from krossdb.core.exceptions import ConfigurationError


class ColumnType(StrEnum):
    """Portable column types, each mapped to a concrete SQLAlchemy type by the builder."""

    STRING = "string"
    TEXT = "text"
    INTEGER = "integer"
    BIGINT = "bigint"
    FLOAT = "float"
    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    DATE = "date"
    DATETIME = "datetime"
    UUID = "uuid"
    JSON = "json"


class ColumnSpec(BaseModel):
    """One column: name, portable type, and the constraints DDL needs to know about."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: ColumnType
    length: int | None = Field(default=None, description="For STRING columns; defaults to 255")
    precision: int | None = Field(default=None, description="For NUMERIC columns")
    scale: int | None = Field(default=None, description="For NUMERIC columns")
    required: bool = Field(default=False, description="NOT NULL when true")
    primary_key: bool = False
    unique: bool = False
    default: str | int | float | bool | None = Field(
        default=None,
        description="Server-side default. The literal 'now' maps to CURRENT_TIMESTAMP "
        "for DATETIME columns; any other value is passed through as a literal server_default.",
    )


class ForeignKeySpec(BaseModel):
    """A composite-capable foreign key from this table's columns to another table's."""

    model_config = ConfigDict(frozen=True)

    columns: list[str] = Field(min_length=1)
    ref_table: str
    ref_columns: list[str] = Field(min_length=1)
    on_delete: str | None = Field(default=None, description="e.g. 'CASCADE', 'SET NULL'")

    @model_validator(mode="after")
    def _matching_column_counts(self) -> ForeignKeySpec:
        if len(self.columns) != len(self.ref_columns):
            raise ValueError(
                f"foreign key on {self.columns} has {len(self.columns)} column(s) but "
                f"references {len(self.ref_columns)} column(s) on {self.ref_table!r}"
            )
        return self


class IndexSpec(BaseModel):
    """A (possibly unique) index over one or more columns of the enclosing table."""

    model_config = ConfigDict(frozen=True)

    name: str | None = Field(default=None, description="Defaults to 'ix_<table>_<columns>'")
    columns: list[str] = Field(min_length=1)
    unique: bool = False


class TableSpec(BaseModel):
    """One table: its columns plus the foreign keys and indexes defined on it."""

    model_config = ConfigDict(frozen=True)

    name: str
    columns: list[ColumnSpec] = Field(min_length=1)
    foreign_keys: list[ForeignKeySpec] = Field(default_factory=list)
    indexes: list[IndexSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_references(self) -> TableSpec:
        column_names = {c.name for c in self.columns}
        if not any(c.primary_key for c in self.columns):
            raise ValueError(f"table {self.name!r} has no primary_key column")
        for fk in self.foreign_keys:
            missing = set(fk.columns) - column_names
            if missing:
                raise ValueError(f"table {self.name!r}: foreign key references undefined column(s) {sorted(missing)}")
        for idx in self.indexes:
            missing = set(idx.columns) - column_names
            if missing:
                raise ValueError(f"table {self.name!r}: index references undefined column(s) {sorted(missing)}")
        return self


class SchemaConfig(BaseModel):
    """The full declarative schema: every table, in dependency-safe creation order."""

    model_config = ConfigDict(frozen=True)

    tables: list[TableSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_fk_targets_exist(self) -> SchemaConfig:
        table_names = {t.name for t in self.tables}
        for table in self.tables:
            for fk in table.foreign_keys:
                if fk.ref_table not in table_names:
                    raise ValueError(
                        f"table {table.name!r}: foreign key references unknown table {fk.ref_table!r}"
                    )
        return self


def load_schema_config(path: str | Path) -> SchemaConfig:
    """Load a :class:`SchemaConfig` from a YAML or JSON file (by extension).

    Raises :class:`~krossdb.core.exceptions.ConfigurationError` for a missing file, a
    parse failure, or a schema that fails validation, so callers never need to catch
    Pydantic/YAML/JSON exceptions directly.
    """

    file_path = Path(path)
    if not file_path.exists():
        raise ConfigurationError(f"schema config not found: {file_path}")

    text = file_path.read_text(encoding="utf-8")
    try:
        if file_path.suffix.lower() in (".yaml", ".yml"):
            import yaml

            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except Exception as exc:
        raise ConfigurationError(f"failed to parse schema config {file_path}: {exc}", cause=exc) from exc

    try:
        return SchemaConfig.model_validate(raw)
    except Exception as exc:
        raise ConfigurationError(f"invalid schema config {file_path}: {exc}", cause=exc) from exc


__all__ = [
    "ColumnSpec",
    "ColumnType",
    "ForeignKeySpec",
    "IndexSpec",
    "SchemaConfig",
    "TableSpec",
    "load_schema_config",
]
