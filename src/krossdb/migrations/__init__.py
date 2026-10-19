"""Generic schema planning, versioned migrations and optional Alembic wiring.

See :mod:`krossdb.migrations.support` for the ``env.py`` helper, and
``krossdb schema init-migrations`` for scaffolding a starter Alembic project.
Schema planning and numbered manifests require the ``migrations`` extra.
"""

from __future__ import annotations

from krossdb.migrations.planner import SchemaChange, SchemaPlan, apply_schema, plan_schema
from krossdb.migrations.runner import MigrationRunner, MigrationSpec, load_migrations
from krossdb.migrations.support import configure, sync_dsn_from_async

__all__ = [
    "MigrationRunner", "MigrationSpec", "SchemaChange", "SchemaPlan",
    "apply_schema", "configure", "load_migrations", "plan_schema", "sync_dsn_from_async",
]
