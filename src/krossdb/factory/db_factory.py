"""Dynamic factory: builds and owns every configured adapter.

Concrete adapter classes register themselves against a :class:`DatabaseKind`
(typically in their module's bottom via :func:`register_adapter`, imported
lazily so an unused driver never needs to be installed). The factory is the
single place that turns a list of :class:`ConnectionConfig` into live,
connected adapters, and is what application code / the CLI depends on instead
of importing adapters directly.
"""

from __future__ import annotations

from typing import Any, Self

from krossdb.core.config import ConnectionConfig
from krossdb.core.enums import DatabaseKind
from krossdb.core.exceptions import ConfigurationError
from krossdb.core.logging import get_logger
from krossdb.interfaces.adapter import AbstractDatabaseAdapter, HealthCheckResult

_logger = get_logger("krossdb.factory")

_ADAPTER_REGISTRY: dict[DatabaseKind, type[AbstractDatabaseAdapter[Any]]] = {}


def register_adapter(kind: DatabaseKind, adapter_cls: type[AbstractDatabaseAdapter[Any]]) -> None:
    """Register a concrete adapter implementation for a database kind.

    Called by each adapter module on import (e.g. ``krossdb.adapters.relational.postgres``
    calls ``register_adapter(DatabaseKind.POSTGRESQL, PostgresAdapter)`` at module scope).
    """

    _ADAPTER_REGISTRY[kind] = adapter_cls


def _resolve_adapter_class(kind: DatabaseKind) -> type[AbstractDatabaseAdapter[Any]]:
    if kind in _ADAPTER_REGISTRY:
        return _ADAPTER_REGISTRY[kind]

    # Lazy-import the adapter module so installing e.g. only `krossdb[postgres]`
    # doesn't require mongodb/qdrant drivers to be present at import time.
    module_by_kind = {
        DatabaseKind.POSTGRESQL: "krossdb.adapters.relational.postgres",
        DatabaseKind.MYSQL: "krossdb.adapters.relational.mysql",
        DatabaseKind.SQLITE: "krossdb.adapters.relational.sqlite",
        DatabaseKind.MONGODB: "krossdb.adapters.document.mongodb",
        DatabaseKind.QDRANT: "krossdb.adapters.vector.qdrant",
    }
    module_path = module_by_kind.get(kind)
    if module_path is None:
        raise ConfigurationError(f"No adapter is registered or known for database kind {kind!r}")

    try:
        import importlib

        importlib.import_module(module_path)
    except ImportError as exc:
        raise ConfigurationError(
            f"Adapter for {kind!r} requires optional dependencies that are not installed "
            f"(tried importing {module_path!r}). Install with `pip install krossdb[{kind}]`.",
            cause=exc,
        ) from exc

    if kind not in _ADAPTER_REGISTRY:
        raise ConfigurationError(f"Module {module_path!r} did not register an adapter for {kind!r}")
    return _ADAPTER_REGISTRY[kind]


class DatabaseFactory:
    """Owns the lifecycle of every configured logical database connection.

    Example::

        factory = DatabaseFactory([pg_config, mongo_config, qdrant_config])
        async with factory:
            pg_adapter = factory.get("primary_pg")
            mongo_adapter = factory.get("events_store")
    """

    def __init__(self, configs: list[ConnectionConfig]) -> None:
        names = [c.name for c in configs]
        if len(names) != len(set(names)):
            raise ConfigurationError(f"Duplicate connection names in factory config: {names}")
        self._configs = {c.name: c for c in configs}
        self._adapters: dict[str, AbstractDatabaseAdapter[Any]] = {}

    async def connect_all(self) -> None:
        for name, config in self._configs.items():
            adapter_cls = _resolve_adapter_class(config.kind)
            adapter = adapter_cls(config)
            await adapter.connect()
            self._adapters[name] = adapter
            _logger.info("factory.adapter_connected", name=name, kind=str(config.kind))

    async def disconnect_all(self) -> None:
        for name, adapter in self._adapters.items():
            try:
                await adapter.disconnect()
                _logger.info("factory.adapter_disconnected", name=name)
            except Exception as exc:
                _logger.error("factory.disconnect_failed", name=name, error=str(exc))
        self._adapters.clear()

    def get(self, name: str) -> AbstractDatabaseAdapter[Any]:
        try:
            return self._adapters[name]
        except KeyError as exc:
            if name in self._configs:
                raise ConfigurationError(
                    f"Connection {name!r} is configured but not connected; "
                    "call connect_all() or use the factory as an async context manager."
                ) from exc
            raise ConfigurationError(f"No connection named {name!r} is configured") from exc

    def __contains__(self, name: str) -> bool:
        return name in self._adapters

    async def health_check_all(self) -> dict[str, HealthCheckResult]:
        results: dict[str, HealthCheckResult] = {}
        for name, adapter in self._adapters.items():
            results[name] = await adapter.health_check()
        return results

    async def __aenter__(self) -> Self:
        await self.connect_all()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect_all()


__all__ = ["DatabaseFactory", "register_adapter"]
