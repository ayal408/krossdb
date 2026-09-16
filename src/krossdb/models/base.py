"""Base Pydantic models shared by domain entities across all adapters."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Entity(BaseModel):
    """Base for every domain entity persisted through a repository.

    Provides the fields krossdb's cross-cutting concerns rely on: a stable
    ``id``, audit timestamps, an optional ``tenant_id`` for multi-tenant
    isolation (see :mod:`krossdb.multitenancy`), and a version counter for
    optimistic concurrency control.
    """

    model_config = ConfigDict(
        from_attributes=True,
        validate_assignment=True,
        extra="forbid",
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    tenant_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: int = Field(default=1, ge=1, description="Optimistic-concurrency version counter")


class AuditRecord(BaseModel):
    """A single audit-trail entry, written automatically by instrumented repositories."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    entity_type: str
    entity_id: str
    action: str
    actor: str | None = None
    tenant_id: str | None = None
    correlation_id: str | None = None
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = ["AuditRecord", "Entity"]
