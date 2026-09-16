"""Domain events, dispatcher contract, and the Outbox persistence contract."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

EventHandler = Callable[["DomainEvent"], Awaitable[None]]


class DomainEvent(BaseModel):
    """Base class for all domain events raised by repositories/UoW.

    Subclass per event type, e.g.::

        class OrderPlaced(DomainEvent):
            order_id: UUID
            total_cents: int
    """

    model_config = ConfigDict(frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = None
    tenant_id: str | None = None

    @property
    def event_type(self) -> str:
        return type(self).__name__


class OutboxRecord(BaseModel):
    """Row/document shape for the transactional outbox table."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None
    attempts: int = 0


class AbstractOutboxStore(ABC):
    """Durable storage for events written in the same transaction as the business data.

    A background relay (not part of this ABC) polls :meth:`fetch_unpublished`
    and calls :meth:`mark_published`, giving at-least-once delivery to the
    :class:`AbstractEventDispatcher` without a distributed transaction / 2PC.
    """

    @abstractmethod
    async def save(self, records: list[OutboxRecord]) -> None: ...

    @abstractmethod
    async def fetch_unpublished(self, *, limit: int = 100) -> list[OutboxRecord]: ...

    @abstractmethod
    async def mark_published(self, record_ids: list[uuid.UUID]) -> None: ...


class AbstractEventDispatcher(ABC):
    """In-process pub/sub for domain events, plus the hook point for the outbox relay."""

    @abstractmethod
    def subscribe(self, event_type: type[DomainEvent], handler: EventHandler) -> None: ...

    @abstractmethod
    async def publish(self, event: DomainEvent) -> None: ...

    @abstractmethod
    async def publish_many(self, events: list[DomainEvent]) -> None: ...


__all__ = [
    "AbstractEventDispatcher",
    "AbstractOutboxStore",
    "DomainEvent",
    "EventHandler",
    "OutboxRecord",
]
