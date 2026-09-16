"""krossdb: a unified, enterprise-grade multi-database interface for Python.

Public surface is re-exported here so application code can do::

    from krossdb import AbstractRepository, ConnectionConfig, DatabaseFactory, KrossDBError
"""

from __future__ import annotations

from krossdb.core import exception_mapper as _exception_mapper  # noqa: F401  (bootstraps mappers)
from krossdb.core.config import (
    CircuitBreakerConfig,
    ConnectionConfig,
    KrossDBSettings,
    NodeConfig,
    PoolConfig,
    RetryConfig,
)
from krossdb.core.context import correlation_scope, get_correlation_id, tenant_scope
from krossdb.core.enums import DatabaseKind, HealthStatus, RoutingRole
from krossdb.core.exceptions import (
    BulkOperationError,
    KrossDBError,
    RecordNotFoundError,
    TenantContextMissingError,
)
from krossdb.core.logging import configure_logging, get_logger
from krossdb.factory.db_factory import DatabaseFactory, register_adapter
from krossdb.interfaces.adapter import AbstractDatabaseAdapter, HealthCheckResult
from krossdb.interfaces.cache import AbstractCache, cache_aside
from krossdb.interfaces.events import AbstractEventDispatcher, AbstractOutboxStore, DomainEvent
from krossdb.interfaces.repository import AbstractRepository, BulkResult, Page, SortSpec
from krossdb.interfaces.unit_of_work import AbstractUnitOfWork
from krossdb.models.base import AuditRecord, Entity

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # config / settings
    "ConnectionConfig",
    "KrossDBSettings",
    "NodeConfig",
    "PoolConfig",
    "RetryConfig",
    "CircuitBreakerConfig",
    # context
    "correlation_scope",
    "get_correlation_id",
    "tenant_scope",
    # enums
    "DatabaseKind",
    "HealthStatus",
    "RoutingRole",
    # exceptions
    "KrossDBError",
    "RecordNotFoundError",
    "BulkOperationError",
    "TenantContextMissingError",
    # logging
    "configure_logging",
    "get_logger",
    # factory
    "DatabaseFactory",
    "register_adapter",
    # interfaces
    "AbstractDatabaseAdapter",
    "HealthCheckResult",
    "AbstractCache",
    "cache_aside",
    "AbstractEventDispatcher",
    "AbstractOutboxStore",
    "DomainEvent",
    "AbstractRepository",
    "BulkResult",
    "Page",
    "SortSpec",
    "AbstractUnitOfWork",
    # models
    "Entity",
    "AuditRecord",
]
