"""Shared enumerations used across adapters, repositories, and the factory."""

from __future__ import annotations

from enum import StrEnum, auto


class DatabaseFamily(StrEnum):
    """Broad category of a database backend, used for adapter dispatch."""

    RELATIONAL = auto()
    DOCUMENT = auto()
    VECTOR = auto()


class DatabaseKind(StrEnum):
    """Concrete database engine identifiers."""

    POSTGRESQL = auto()
    MYSQL = auto()
    SQLITE = auto()
    MONGODB = auto()
    QDRANT = auto()


class RoutingRole(StrEnum):
    """Role of a node within a read/write-split relational cluster."""

    MASTER = auto()
    REPLICA = auto()


class IsolationLevel(StrEnum):
    """Transaction isolation levels, mapped to driver-specific strings by adapters."""

    READ_UNCOMMITTED = "READ UNCOMMITTED"
    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"
    SERIALIZABLE = "SERIALIZABLE"


class CircuitState(StrEnum):
    """State machine positions for the circuit breaker."""

    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class HealthStatus(StrEnum):
    """Result of an adapter health check."""

    HEALTHY = auto()
    DEGRADED = auto()
    UNHEALTHY = auto()
