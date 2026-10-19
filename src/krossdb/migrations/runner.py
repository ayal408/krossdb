"""Project-owned numbered migrations, with checksums, locking and atomic history."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine

from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import (
    ConfigurationError,
    TransactionAlreadyActiveError,
    UnsupportedOperationError,
)
from krossdb.migrations.planner import apply_schema
from krossdb.schema import SchemaConfig, build_metadata

_HISTORY_NAME = "krossdb_schema_migrations"
_LOCK_KEY = 648235683421


class MigrationSpec(BaseModel):
    """One immutable project revision. SQL is trusted, developer-authored SQL.

    Statements run before the optional schema snapshot, allowing a backfill
    before adding a required constraint. Each SQL item is one driver statement.
    Backend-specific SQL must declare its dialect explicitly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(gt=0, strict=True)
    name: str = Field(min_length=1, max_length=255)
    dialect: str | None = None
    sql: tuple[str, ...] = ()
    schema_config: SchemaConfig | None = Field(default=None, alias="schema")

    @model_validator(mode="after")
    def _validate_operations(self) -> Self:
        if not self.name.strip() or (not self.sql and self.schema_config is None):
            raise ValueError("Migration requires a name and SQL or a schema")
        if self.sql and self.dialect not in {"postgresql", "sqlite"}:
            raise ValueError("SQL migrations must declare dialect: postgresql or sqlite")
        for statement in self.sql:
            if not statement.strip():
                raise ValueError("SQL statements cannot be empty")
        if self.schema_config and any(t.name == _HISTORY_NAME for t in self.schema_config.tables):
            raise ValueError(f"{_HISTORY_NAME} is reserved for migration history")
        return self

    @property
    def checksum(self) -> str:
        # Hash authored fields, so future library fields with defaults do not
        # invalidate the checksums of unchanged project revision files.
        payload = json.dumps(self.model_dump(mode="json", by_alias=True, exclude_unset=True), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class MigrationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    migrations: list[MigrationSpec]


def load_migrations(path: str | Path) -> list[MigrationSpec]:
    """Load a project-owned JSON/YAML manifest, without importing application code."""
    import yaml

    file_path = Path(path)
    try:
        content = file_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(content) if file_path.suffix.lower() in {".yml", ".yaml"} else json.loads(content)
        return MigrationManifest.model_validate(raw).migrations
    except Exception as exc:
        raise ConfigurationError(f"Invalid migration manifest {file_path}: {exc}", cause=exc) from exc


class MigrationRunner:
    """One version stream per database; accepts no application/domain definitions.

    ``pending`` is read-only. ``apply`` requires an idle SQLAlchemy Connection
    and owns a single transaction for all pending revisions plus their history.
    PostgreSQL uses a transaction advisory lock; SQLite uses BEGIN IMMEDIATE.
    MySQL's implicit DDL commits cannot provide this guarantee: use Alembic there.
    """

    def __init__(self, migrations: Sequence[MigrationSpec], *, lock_timeout_ms: int = 30_000) -> None:
        if lock_timeout_ms <= 0:
            raise ConfigurationError("lock_timeout_ms must be positive")
        self.migrations = tuple(migrations)
        versions = [m.version for m in migrations]
        if versions != sorted(set(versions)):
            raise ConfigurationError("Migration versions must be unique and increasing")
        self.lock_timeout_ms = lock_timeout_ms
        self.history = Table(
            _HISTORY_NAME, MetaData(),
            Column("version", Integer, primary_key=True),
            Column("name", String(255), nullable=False),
            Column("checksum", String(64), nullable=False),
            Column("applied_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
        )

    def pending(self, connection: Connection) -> tuple[MigrationSpec, ...]:
        """Read history and validate its exact prefix/checksums; never create tables."""
        with translate_exceptions(op="migration_plan"):
            rows = []
            if inspect(connection).has_table(_HISTORY_NAME):
                rows = list(connection.execute(select(self.history).order_by(self.history.c.version)).mappings())
            if len(rows) > len(self.migrations):
                raise ConfigurationError("Database has migrations missing from this manifest")
            for row, migration in zip(rows, self.migrations, strict=False):
                if row["version"] != migration.version or row["checksum"] != migration.checksum:
                    raise ConfigurationError(f"Migration history mismatch at version {row['version']}; restore the original manifest")
            pending = self.migrations[len(rows):]
            for migration in pending:
                if migration.dialect and migration.dialect != connection.dialect.name:
                    raise UnsupportedOperationError(f"Migration {migration.version} requires {migration.dialect}")
            return pending

    @staticmethod
    def _validate_sql(statement: str) -> None:
        try:
            import sqlparse
        except ImportError as exc:
            raise ConfigurationError("Install krossdb[migrations] for SQL migrations") from exc
        parsed: Any = sqlparse.parse(statement)
        if len(parsed) != 1:
            raise ConfigurationError("Each sql entry must contain exactly one statement")
        first = parsed[0].token_first(skip_cm=True)
        keyword = re.match(r"[A-Za-z]+", first.value) if first else None
        if keyword is None or keyword.group().upper() in {
            "BEGIN", "START", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE",
            "ABORT", "PREPARE", "VACUUM", "ATTACH", "DETACH", "PRAGMA",
        }:
            raise ConfigurationError("Transaction control and nontransactional SQL are not allowed in migrations")

    def apply(self, connection: Connection) -> tuple[int, ...]:
        """Recheck history after acquiring the lock; commit only after every revision succeeds."""
        if connection.in_transaction():
            raise TransactionAlreadyActiveError("Use an idle connection; the migration runner owns its transaction")
        dialect = connection.dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise UnsupportedOperationError("Atomic migrations support PostgreSQL and SQLite; use Alembic for other dialects")
        dbapi = connection.connection.dbapi_connection
        if dialect == "postgresql" and getattr(dbapi, "autocommit", False):
            raise ConfigurationError("Migrations cannot use AUTOCOMMIT")
        if dialect == "postgresql" and connection.get_isolation_level() != "READ COMMITTED":
            raise ConfigurationError("Use READ COMMITTED so history is refreshed after acquiring the migration lock")
        # Validate every statement before executing any revision.
        for migration in self.migrations:
            for statement in migration.sql:
                self._validate_sql(statement)
        with translate_exceptions(op="migrate"), connection.begin():
            if dialect == "postgresql":
                connection.execute(text("SELECT set_config('lock_timeout', :timeout, true)"), {"timeout": f"{self.lock_timeout_ms}ms"})
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY})
            else:
                # Explicit BEGIN is essential: sqlite3's legacy mode otherwise
                # autocommits DDL, even inside SQLAlchemy's logical transaction.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            pending = self.pending(connection)
            if not pending:
                return ()
            self.history.create(connection, checkfirst=True)
            for migration in pending:
                for statement in migration.sql:
                    connection.exec_driver_sql(statement)
                if migration.schema_config:
                    apply_schema(connection, build_metadata(migration.schema_config))
                connection.execute(self.history.insert().values(
                    version=migration.version, name=migration.name, checksum=migration.checksum,
                ))
            return tuple(m.version for m in pending)

    async def apply_async(self, engine: AsyncEngine) -> tuple[int, ...]:
        """Use the same runner with an async SQLAlchemy engine and its native driver."""
        async with engine.connect() as connection:
            return await connection.run_sync(self.apply)


__all__ = ["MigrationRunner", "MigrationSpec", "load_migrations"]
