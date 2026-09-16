"""In-process pub/sub dispatcher for domain events."""

from __future__ import annotations

from collections import defaultdict

from krossdb.core.logging import get_logger
from krossdb.interfaces.events import AbstractEventDispatcher, DomainEvent, EventHandler

_logger = get_logger("krossdb.events.dispatcher")


class InMemoryEventDispatcher(AbstractEventDispatcher):
    """Simple in-process dispatcher: subscribers run sequentially on publish.

    A handler's exception is logged and swallowed rather than propagated, so
    one misbehaving subscriber can't break the publisher or other
    subscribers. For at-least-once delivery across process restarts, pair
    this with the outbox relay (see :mod:`krossdb.events.outbox`) instead of
    relying on in-process publish alone.
    """

    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: type[DomainEvent], handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        for handler in self._handlers.get(type(event), []):
            try:
                await handler(event)
            except Exception as exc:
                _logger.error(
                    "event_dispatcher.handler_failed",
                    event_type=event.event_type,
                    event_id=str(event.event_id),
                    error=str(exc),
                )

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)


__all__ = ["InMemoryEventDispatcher"]
