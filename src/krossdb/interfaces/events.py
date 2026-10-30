"""Domain events, dispatcher contract, and the Outbox persistence contract."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
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

    :class:`~krossdb.events.relay.OutboxRelay` drains it: inside :meth:`claim`
    it calls :meth:`fetch_unpublished`, publishes each record, then calls
    :meth:`mark_published` / :meth:`record_failed_attempts`. That gives
    at-least-once delivery without a distributed transaction / 2PC.
    """

    @abstractmethod
    async def save(self, records: list[OutboxRecord]) -> None: ...

    @abstractmethod
    async def fetch_unpublished(
        self, *, limit: int = 100, max_attempts: int | None = None
    ) -> list[OutboxRecord]:
        """Oldest unpublished records first, skipping any with ``attempts >= max_attempts``."""

    @abstractmethod
    async def mark_published(self, record_ids: list[uuid.UUID]) -> None: ...

    async def record_failed_attempts(self, record_ids: list[uuid.UUID]) -> None:
        """Increment ``attempts`` for records whose publish failed."""

        raise NotImplementedError(
            f"{type(self).__name__} does not track failed publish attempts; "
            "override record_failed_attempts() to use it with OutboxRelay"
        )

    @asynccontextmanager
    async def claim(self) -> AsyncIterator[None]:
        """Scope for one relay batch: fetch, publish, and mark.

        Stores that can lock rows override this so concurrent relays never
        pick up the same records. The default does no locking, which is only
        safe with a single relay.
        """

        yield


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
