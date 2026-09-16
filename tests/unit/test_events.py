"""Unit tests for the in-memory event dispatcher and outbox store."""

from __future__ import annotations

import pytest

from krossdb.events.dispatcher import InMemoryEventDispatcher
from krossdb.events.outbox import InMemoryOutboxStore
from krossdb.interfaces.events import DomainEvent, OutboxRecord

pytestmark = pytest.mark.unit


class WidgetCreated(DomainEvent):
    widget_id: str


class WidgetDeleted(DomainEvent):
    widget_id: str


async def test_publish_calls_only_subscribers_of_the_matching_event_type():
    dispatcher = InMemoryEventDispatcher()
    created_events: list[DomainEvent] = []
    deleted_events: list[DomainEvent] = []

    async def on_created(event: DomainEvent) -> None:
        created_events.append(event)

    async def on_deleted(event: DomainEvent) -> None:
        deleted_events.append(event)

    dispatcher.subscribe(WidgetCreated, on_created)
    dispatcher.subscribe(WidgetDeleted, on_deleted)

    await dispatcher.publish(WidgetCreated(widget_id="1"))

    assert len(created_events) == 1
    assert len(deleted_events) == 0


async def test_publish_with_no_subscribers_does_not_raise():
    dispatcher = InMemoryEventDispatcher()

    await dispatcher.publish(WidgetCreated(widget_id="1"))  # should not raise


async def test_multiple_subscribers_all_receive_the_event():
    dispatcher = InMemoryEventDispatcher()
    calls: list[str] = []

    async def handler_a(event: DomainEvent) -> None:
        calls.append("a")

    async def handler_b(event: DomainEvent) -> None:
        calls.append("b")

    dispatcher.subscribe(WidgetCreated, handler_a)
    dispatcher.subscribe(WidgetCreated, handler_b)

    await dispatcher.publish(WidgetCreated(widget_id="1"))

    assert calls == ["a", "b"]


async def test_a_failing_handler_does_not_prevent_others_from_running():
    dispatcher = InMemoryEventDispatcher()
    calls: list[str] = []

    async def failing_handler(event: DomainEvent) -> None:
        raise RuntimeError("boom")

    async def ok_handler(event: DomainEvent) -> None:
        calls.append("ok")

    dispatcher.subscribe(WidgetCreated, failing_handler)
    dispatcher.subscribe(WidgetCreated, ok_handler)

    await dispatcher.publish(WidgetCreated(widget_id="1"))  # should not raise

    assert calls == ["ok"]


async def test_publish_many_dispatches_every_event_in_order():
    dispatcher = InMemoryEventDispatcher()
    seen: list[str] = []

    async def handler(event: WidgetCreated) -> None:
        seen.append(event.widget_id)

    dispatcher.subscribe(WidgetCreated, handler)

    await dispatcher.publish_many([WidgetCreated(widget_id="1"), WidgetCreated(widget_id="2")])

    assert seen == ["1", "2"]


async def test_outbox_fetch_unpublished_only_returns_unpublished_records():
    outbox = InMemoryOutboxStore()
    record = OutboxRecord(event_type="WidgetCreated", payload={"widget_id": "1"})
    await outbox.save([record])

    unpublished = await outbox.fetch_unpublished()

    assert len(unpublished) == 1
    assert unpublished[0].id == record.id


async def test_outbox_mark_published_excludes_record_from_future_fetches():
    outbox = InMemoryOutboxStore()
    record = OutboxRecord(event_type="WidgetCreated", payload={"widget_id": "1"})
    await outbox.save([record])

    await outbox.mark_published([record.id])

    assert await outbox.fetch_unpublished() == []


async def test_outbox_fetch_unpublished_respects_limit():
    outbox = InMemoryOutboxStore()
    records = [OutboxRecord(event_type="WidgetCreated", payload={"i": i}) for i in range(5)]
    await outbox.save(records)

    page = await outbox.fetch_unpublished(limit=2)

    assert len(page) == 2
