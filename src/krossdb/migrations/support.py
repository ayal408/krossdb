"""Alembic environment wiring: bridges a consuming app's ``alembic/env.py`` to
krossdb's own ``krossdb.yaml`` connection config.

krossdb does not reimplement Alembic. A consuming app still owns its own
``alembic/`` directory and uses the standard ``alembic`` CLI for day-to-day
work (``alembic revision --autogenerate``, ``alembic upgrade head``, ...).
This module's only job is resolving the URL Alembic connects with (and the
target ``MetaData``) from krossdb's config instead of a hardcoded
``sqlalchemy.url`` in ``alembic.ini``, using the same
``module.path:attribute_name`` import convention as ``krossdb schema create``.

Alembic drives migrations through a *sync* SQLAlchemy engine even in
"online" mode, so the async DSN scheme every other krossdb adapter uses
(``postgresql+asyncpg://``, ``mysql+asyncmy://``, ``sqlite+aiosqlite://``)
has to be rewritten to a sync-capable one before Alembic ever sees it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from krossdb.core.config import ConnectionConfig
from krossdb.core.exceptions import ConfigurationError

if TYPE_CHECKING:
    from sqlalchemy import MetaData

_SYNC_DRIVER_MAP: dict[str, str] = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "mysql+asyncmy": "mysql+pymysql",
    "mysql": "mysql+pymysql",
    "sqlite+aiosqlite": "sqlite",
}


def sync_dsn_from_async(dsn: str) -> str:
    """Rewrite an async SQLAlchemy DSN scheme to a sync-capable one for Alembic.

    Only the scheme is touched; host/credentials/path are passed through
    verbatim. Unknown schemes raise rather than silently handing Alembic a
    DSN it can't load a DBAPI for.
    """

    scheme, sep, rest = dsn.partition("://")
    if not sep:
        raise ConfigurationError(f"DSN {dsn!r} is missing a '://' scheme separator")

    sync_scheme = _SYNC_DRIVER_MAP.get(scheme)
    if sync_scheme is None:
        raise ConfigurationError(
            f"No known sync-driver mapping for scheme {scheme!r}; add one to "
            "krossdb.migrations.support._SYNC_DRIVER_MAP or pass an already-sync DSN "
            "via krossdb.yaml for migration purposes."
        )

    if sync_scheme == "sqlite":
        return f"sqlite://{rest}"
    return f"{sync_scheme}://{rest}"


def _load_master_dsn(config_path: Path, connection_name: str) -> str:
    import yaml

    if not config_path.exists():
        raise ConfigurationError(f"Config file not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    configs = [ConnectionConfig.model_validate(c) for c in raw.get("connections", [])]
    by_name = {c.name: c for c in configs}

    connection_config = by_name.get(connection_name)
    if connection_config is None:
        raise ConfigurationError(
            f"No connection named {connection_name!r} is configured in {config_path}"
        )

    master_nodes = connection_config.master_nodes
    if not master_nodes:
        raise ConfigurationError(f"Connection {connection_name!r} has no MASTER node configured")

    return master_nodes[0].dsn.get_secret_value()


def configure(
    metadata: MetaData,
    connection_name: str,
    config_path: str | Path = "krossdb.yaml",
) -> None:
    """Wire the calling ``alembic/env.py`` to krossdb's own connection config.

    Call this once from ``env.py`` in place of Alembic's generated
    ``run_migrations_offline``/``run_migrations_online`` boilerplate; it
    resolves ``connection_name``'s master node DSN from ``config_path``,
    converts it to a sync-capable DSN, and runs whichever of Alembic's
    offline/online flows applies (``context.is_offline_mode()`` decides,
    exactly as the default template does).
    """

    try:
        from alembic import context
    except ImportError as exc:
        raise ConfigurationError(
            "Alembic migration support requires the 'migrations' extra: pip install krossdb[migrations]"
        ) from exc

    sync_url = sync_dsn_from_async(_load_master_dsn(Path(config_path), connection_name))
    context.config.set_main_option("sqlalchemy.url", sync_url)

    if context.is_offline_mode():
        context.configure(
            url=sync_url,
            target_metadata=metadata,
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    from sqlalchemy import engine_from_config, pool

    section: dict[str, Any] = context.config.get_section(context.config.config_ini_section) or {}
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()


__all__ = ["configure", "sync_dsn_from_async"]
