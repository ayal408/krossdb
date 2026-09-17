"""Optional Alembic migration wiring (requires the ``migrations`` extra).

See :mod:`krossdb.migrations.support` for the ``env.py`` helper, and
``krossdb schema init-migrations`` for scaffolding a starter Alembic project.
"""

from __future__ import annotations

from krossdb.migrations.support import configure, sync_dsn_from_async

__all__ = ["configure", "sync_dsn_from_async"]
