"""Central translation layer from driver-specific exceptions to :mod:`krossdb` ones.

Each adapter registers its driver's exception types against a small set of
semantic buckets (connection, integrity, timeout, ...). This keeps driver
imports optional (import errors are swallowed) and keeps the mapping logic in
one auditable place instead of scattered ``try/except`` blocks per adapter.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any

from krossdb.core import exceptions as exc

# A mapper takes the raw driver exception and returns the constructed
# KrossDBError, or None if it doesn't recognize the exception (falls through
# to the next registered mapper / the generic fallback).
_MapperFn = Callable[[BaseException], "exc.KrossDBError | None"]

_registry: list[tuple[str, _MapperFn]] = []


def register_mapper(name: str, mapper: _MapperFn) -> None:
    """Register a mapper function under ``name`` (idempotent by name)."""

    _registry[:] = [(n, m) for n, m in _registry if n != name]
    _registry.append((name, mapper))


def _map_exception(error: BaseException, *, context: dict[str, Any] | None = None) -> exc.KrossDBError:
    for _, mapper in _registry:
        try:
            mapped = mapper(error)
        except Exception:  # a buggy mapper must never break error handling
            continue
        if mapped is not None:
            mapped.context.update(context or {})
            mapped.cause = mapped.cause or error
            return mapped

    fallback = exc.QueryError(str(error) or type(error).__name__, cause=error, context=context)
    return fallback


@contextmanager
def translate_exceptions(**context: Any) -> Any:
    """Context manager that translates any raised driver exception in-place.

    Usage::

        with translate_exceptions(adapter="postgres", op="insert"):
            await conn.execute(stmt)
    """

    try:
        yield
    except exc.KrossDBError:
        raise
    except Exception as raw_error:
        raise _map_exception(raw_error, context=context) from raw_error


def translate_sync(fn_error: BaseException, **context: Any) -> exc.KrossDBError:
    """Non-context-manager helper for translating an already-caught exception."""

    return _map_exception(fn_error, context=context)


# --------------------------------------------------------------------------
# Built-in mapper: stdlib / generic transport errors that every adapter can hit
# --------------------------------------------------------------------------


def _generic_mapper(error: BaseException) -> exc.KrossDBError | None:
    if isinstance(error, TimeoutError):
        return exc.TimeoutError_(str(error) or "operation timed out")
    if isinstance(error, ConnectionError):
        return exc.ConnectionError_(str(error) or "connection failed")
    return None


register_mapper("generic", _generic_mapper)


# --------------------------------------------------------------------------
# Optional driver-specific mappers. Each is registered only if the driver is
# installed, so krossdb's core has zero hard dependency on any DB driver.
# --------------------------------------------------------------------------


def _register_asyncpg_mapper() -> None:
    try:
        import asyncpg
    except ImportError:
        return

    def mapper(error: BaseException) -> exc.KrossDBError | None:
        if isinstance(error, asyncpg.UniqueViolationError):
            return exc.DuplicateRecordError(str(error))
        if isinstance(error, asyncpg.ForeignKeyViolationError | asyncpg.CheckViolationError):
            return exc.IntegrityViolationError(str(error))
        if isinstance(error, asyncpg.DeadlockDetectedError):
            return exc.DeadlockDetectedError(str(error))
        if isinstance(error, asyncpg.InvalidPasswordError | asyncpg.InvalidAuthorizationSpecificationError):
            return exc.AuthenticationError(str(error))
        if isinstance(error, asyncpg.TooManyConnectionsError | asyncpg.CannotConnectNowError):
            return exc.ConnectionError_(str(error))
        if isinstance(error, asyncpg.PostgresError):
            return exc.QueryError(str(error))
        return None

    register_mapper("asyncpg", mapper)


def _register_pymongo_mapper() -> None:
    try:
        import pymongo.errors as pymongo_errors
    except ImportError:
        return

    def mapper(error: BaseException) -> exc.KrossDBError | None:
        if isinstance(error, pymongo_errors.DuplicateKeyError):
            return exc.DuplicateRecordError(str(error))
        if isinstance(error, pymongo_errors.ServerSelectionTimeoutError | pymongo_errors.NetworkTimeout):
            return exc.ConnectionTimeoutError(str(error))
        if isinstance(error, pymongo_errors.ConnectionFailure):
            return exc.ConnectionError_(str(error))
        if isinstance(error, pymongo_errors.OperationFailure):
            if error.code in (13, 18):  # Unauthorized, AuthenticationFailed
                return exc.AuthenticationError(str(error))
            return exc.QueryError(str(error))
        if isinstance(error, pymongo_errors.PyMongoError):
            return exc.QueryError(str(error))
        return None

    register_mapper("pymongo", mapper)


def _register_qdrant_mapper() -> None:
    try:
        from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse
    except ImportError:
        return

    def mapper(error: BaseException) -> exc.KrossDBError | None:
        if isinstance(error, UnexpectedResponse):
            status = getattr(error, "status_code", None)
            if status == 404:
                return exc.RecordNotFoundError("qdrant_point", "unknown")
            if status in (401, 403):
                return exc.AuthorizationError(str(error))
            if status == 409:
                return exc.DuplicateRecordError(str(error))
            return exc.QueryError(str(error))
        if isinstance(error, ResponseHandlingException):
            return exc.ConnectionError_(str(error))
        return None

    register_mapper("qdrant", mapper)


def _register_sqlalchemy_mapper() -> None:
    try:
        from sqlalchemy.exc import (
            IntegrityError,
            InterfaceError,
            OperationalError,
        )
        from sqlalchemy.exc import (
            TimeoutError as SATimeoutError,
        )
    except ImportError:
        return

    def mapper(error: BaseException) -> exc.KrossDBError | None:
        if isinstance(error, IntegrityError):
            msg = str(error.orig) if getattr(error, "orig", None) else str(error)
            if "unique" in msg.lower() or "duplicate" in msg.lower():
                return exc.DuplicateRecordError(msg)
            return exc.IntegrityViolationError(msg)
        if isinstance(error, SATimeoutError):
            return exc.ConnectionTimeoutError(str(error))
        if isinstance(error, InterfaceError | OperationalError):
            return exc.ConnectionError_(str(error))
        return None

    register_mapper("sqlalchemy", mapper)


def bootstrap_default_mappers() -> None:
    """Register every optional driver mapper that is currently importable.

    Safe to call multiple times (registration is idempotent per name); called
    automatically on package import.
    """

    _register_sqlalchemy_mapper()
    _register_asyncpg_mapper()
    _register_pymongo_mapper()
    _register_qdrant_mapper()
