"""SQLAlchemy UoW outbox integration tests without a database connection."""

from __future__ import annotations

import pytest

from krossdb.interfaces.events import DomainEvent
from krossdb.uow.sqlalchemy_uow import SQLAlchemyUnitOfWork

pytestmark = pytest.mark.unit


class OrderPlaced(DomainEvent):
    order_id: str


class RecordingOutbox:
    def __init__(self) -> None:
        self.records = []

    async def save(self, records):
        self.records.extend(records)


async def test_registered_events_are_serialized_to_outbox_records():
    store = RecordingOutbox()
    uow = SQLAlchemyUnitOfWork(adapter=None, outbox_store=store)
    event = OrderPlaced(order_id="order-1")
    await uow._persist_events([event])

    assert len(store.records) == 1
    record = store.records[0]
    assert record.id == event.event_id
    assert record.event_type == "OrderPlaced"
    assert record.payload["order_id"] == "order-1"
    assert record.payload["event_id"] == str(event.event_id)
    assert record.created_at == event.occurred_at


async def test_events_without_outbox_store_are_rejected():
    uow = SQLAlchemyUnitOfWork(adapter=None)
    with pytest.raises(NotImplementedError, match="transactional event persistence"):
        await uow._persist_events([OrderPlaced(order_id="order-1")])
