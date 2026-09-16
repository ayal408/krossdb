"""Unified exception hierarchy.

Every driver-specific error (asyncpg, pymongo, qdrant_client, ...) is translated
into one of these types by :mod:`krossdb.core.exception_mapper` so that
application code never needs to import or catch a driver exception directly.
"""

from __future__ import annotations

from typing import Any


class KrossDBError(Exception):
    """Base class for every exception raised by krossdb.

    Carries structured context so it can be logged, serialized, and correlated
    with a trace/correlation id without string-parsing the message.
    """

    def __init__(
        self,
        message: str,
        *,
        cause: BaseException | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.cause = cause
        self.context: dict[str, Any] = context or {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(message={self.message!r}, context={self.context!r})"


# --------------------------------------------------------------------------
# Connectivity / lifecycle
# --------------------------------------------------------------------------


class ConnectionError_(KrossDBError):
    """Failed to establish or maintain a connection to the backend."""


class ConnectionTimeoutError(ConnectionError_):
    """Connection attempt exceeded the configured timeout."""


class CircuitBreakerOpenError(KrossDBError):
    """Calls are being short-circuited because the breaker is open."""


class HealthCheckFailedError(KrossDBError):
    """An adapter's health probe reported an unhealthy backend."""


# --------------------------------------------------------------------------
# Query / data errors
# --------------------------------------------------------------------------


class QueryError(KrossDBError):
    """A query or command failed to execute."""


class IntegrityViolationError(QueryError):
    """A uniqueness, foreign-key, or check constraint was violated."""


class RecordNotFoundError(KrossDBError):
    """The requested entity does not exist."""

    def __init__(self, entity: str, identifier: Any, **kwargs: Any) -> None:
        super().__init__(f"{entity} with id={identifier!r} was not found", **kwargs)
        self.entity = entity
        self.identifier = identifier


class DuplicateRecordError(IntegrityViolationError):
    """An insert/upsert collided with an existing unique key."""


class ValidationError_(KrossDBError):
    """A Pydantic (or adapter-level) schema validation failed."""


class SerializationError(KrossDBError):
    """Data could not be serialized/deserialized between the domain and the wire format."""


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


class TransactionError(KrossDBError):
    """Generic failure inside a Unit of Work transaction."""


class TransactionAlreadyActiveError(TransactionError):
    """Attempted to begin a transaction while one is already in progress."""


class NoActiveTransactionError(TransactionError):
    """Attempted to commit/rollback without an active transaction."""


class DeadlockDetectedError(TransactionError):
    """The backend detected and aborted a transaction to break a deadlock."""


# --------------------------------------------------------------------------
# Authn/z, configuration, tenancy
# --------------------------------------------------------------------------


class AuthenticationError(KrossDBError):
    """Credentials were rejected by the backend."""


class AuthorizationError(KrossDBError):
    """The authenticated principal lacks permission for the operation."""


class ConfigurationError(KrossDBError):
    """The library or an adapter was misconfigured."""


class TenantContextMissingError(KrossDBError):
    """A tenant-scoped operation was attempted with no active tenant context."""


class CrossTenantAccessError(KrossDBError):
    """An operation attempted to touch data outside the caller's tenant."""


# --------------------------------------------------------------------------
# Operational
# --------------------------------------------------------------------------


class TimeoutError_(KrossDBError):
    """An operation exceeded its allotted time budget."""


class UnsupportedOperationError(KrossDBError):
    """The requested capability is not supported by this adapter."""


class IdempotencyConflictError(KrossDBError):
    """An idempotency key was replayed with a different payload."""


class CacheError(KrossDBError):
    """The distributed cache layer failed in a way that should be surfaced."""


class BulkOperationError(KrossDBError):
    """One or more items in a bulk operation failed.

    ``failures`` maps the index of the failed item to the underlying error, so
    callers can implement partial-success handling.
    """

    def __init__(
        self,
        message: str,
        *,
        failures: dict[int, BaseException],
        succeeded: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.failures = failures
        self.succeeded = succeeded


__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "BulkOperationError",
    "CacheError",
    "CircuitBreakerOpenError",
    "ConfigurationError",
    "ConnectionError_",
    "ConnectionTimeoutError",
    "CrossTenantAccessError",
    "DeadlockDetectedError",
    "DuplicateRecordError",
    "HealthCheckFailedError",
    "IdempotencyConflictError",
    "IntegrityViolationError",
    "KrossDBError",
    "NoActiveTransactionError",
    "QueryError",
    "RecordNotFoundError",
    "SerializationError",
    "TenantContextMissingError",
    "TimeoutError_",
    "TransactionAlreadyActiveError",
    "TransactionError",
    "UnsupportedOperationError",
    "ValidationError_",
]
