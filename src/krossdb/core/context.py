"""Ambient request-scoped context: correlation IDs and multi-tenancy.

Implemented with :mod:`contextvars` so values propagate correctly through
``async``/``await`` call chains and across ``asyncio.create_task`` boundaries
(when using ``contextvars.copy_context`` semantics, which asyncio does by
default), without needing to thread parameters through every function.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from krossdb.core.exceptions import TenantContextMissingError

_correlation_id: ContextVar[str | None] = ContextVar("krossdb_correlation_id", default=None)
_tenant_id: ContextVar[str | None] = ContextVar("krossdb_tenant_id", default=None)


def get_correlation_id() -> str:
    """Return the current correlation id, minting a new UUID4 if none is set."""

    current = _correlation_id.get()
    if current is None:
        current = str(uuid.uuid4())
        _correlation_id.set(current)
    return current


def set_correlation_id(value: str) -> Token[str | None]:
    return _correlation_id.set(value)


@contextmanager
def correlation_scope(correlation_id: str | None = None) -> Iterator[str]:
    """Bind a correlation id (generating one if omitted) for the duration of the block."""

    value = correlation_id or str(uuid.uuid4())
    token = _correlation_id.set(value)
    try:
        yield value
    finally:
        _correlation_id.reset(token)


def get_tenant_id(*, required: bool = False) -> str | None:
    tenant = _tenant_id.get()
    if required and tenant is None:
        raise TenantContextMissingError(
            "No tenant context is bound; wrap the call in `tenant_scope(tenant_id)`."
        )
    return tenant


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[str]:
    """Bind a tenant id for the duration of the block, enabling row/collection isolation."""

    if not tenant_id:
        raise TenantContextMissingError("tenant_id must be a non-empty string")
    token = _tenant_id.set(tenant_id)
    try:
        yield tenant_id
    finally:
        _tenant_id.reset(token)


__all__ = [
    "correlation_scope",
    "get_correlation_id",
    "get_tenant_id",
    "set_correlation_id",
    "tenant_scope",
]
