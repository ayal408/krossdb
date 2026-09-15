"""Integration tests against a real MongoDB container (via Testcontainers).

A raw-Motor smoke test proves the fixture/exception-mapper wiring works; the
rest exercise ``MongoDBAdapter`` + ``MongoDBRepository`` end-to-end against
the real server.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("motor")

from nexusdb.adapters.document.mongodb import MongoDBAdapter
from nexusdb.core.config import ConnectionConfig, NodeConfig
from nexusdb.core.enums import DatabaseKind, RoutingRole
from nexusdb.core.exception_mapper import translate_exceptions
from nexusdb.core.exceptions import DuplicateRecordError, RecordNotFoundError
from nexusdb.models.base import Entity
from nexusdb.repositories.document_repository import MongoDBRepository


class Widget(Entity):
    name: str
    quantity: int = 0


async def test_can_connect_and_round_trip_a_document(mongodb_uri: str) -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    client: AsyncIOMotorClient = AsyncIOMotorClient(mongodb_uri)
    try:
        db = client.get_default_database()
        result = await db.raw_widgets_it.insert_one({"name": "widget-a", "quantity": 3})
        doc = await db.raw_widgets_it.find_one({"_id": result.inserted_id})
        assert doc is not None
        assert doc["name"] == "widget-a"
    finally:
        client.close()


async def test_duplicate_key_is_mapped_to_duplicate_record_error(mongodb_uri: str) -> None:
    from motor.motor_asyncio import AsyncIOMotorClient

    client: AsyncIOMotorClient = AsyncIOMotorClient(mongodb_uri)
    try:
        db = client.get_default_database()
        await db.unique_widgets_it.create_index("sku", unique=True)
        await db.unique_widgets_it.insert_one({"sku": "ABC-1"})

        with pytest.raises(DuplicateRecordError), translate_exceptions(op="insert"):
            await db.unique_widgets_it.insert_one({"sku": "ABC-1"})
    finally:
        client.close()


@pytest_asyncio.fixture
async def mongo_adapter(mongodb_uri: str) -> AsyncIterator[MongoDBAdapter]:
    config = ConnectionConfig(
        name="mongo-it",
        kind=DatabaseKind.MONGODB,
        nodes=[NodeConfig(dsn=mongodb_uri, role=RoutingRole.MASTER)],
    )
    adapter = MongoDBAdapter(config)
    await adapter.connect()
    yield adapter
    await adapter.disconnect()


@pytest.fixture
def widget_repo(mongo_adapter: MongoDBAdapter) -> MongoDBRepository:
    return MongoDBRepository(mongo_adapter, "widgets_it", Widget)


async def test_repository_crud_round_trip(widget_repo: MongoDBRepository) -> None:
    widget = Widget(name=f"alpha-{uuid.uuid4()}", quantity=1)

    await widget_repo.create(widget)
    fetched = await widget_repo.get_by_id(widget.id)
    assert fetched is not None and fetched.name == widget.name

    updated = await widget_repo.update(widget.id, {"quantity": 9})
    assert updated.quantity == 9

    assert await widget_repo.delete(widget.id) is True
    assert await widget_repo.get_by_id(widget.id) is None


async def test_repository_update_missing_raises_record_not_found(
    widget_repo: MongoDBRepository,
) -> None:
    with pytest.raises(RecordNotFoundError):
        await widget_repo.update(uuid.uuid4(), {"quantity": 1})


async def test_bulk_create_and_bulk_delete(widget_repo: MongoDBRepository) -> None:
    widgets = [Widget(name=f"bulk-{uuid.uuid4()}", quantity=i) for i in range(3)]

    result = await widget_repo.bulk_create(widgets)
    assert result.all_succeeded
    assert result.success_count == 3

    deleted = await widget_repo.bulk_delete([w.id for w in widgets])
    assert deleted == 3


async def test_find_with_criteria_and_pagination(widget_repo: MongoDBRepository) -> None:
    tag = str(uuid.uuid4())
    for i in range(5):
        await widget_repo.create(Widget(name=f"{tag}-{i}", quantity=1))

    page = await widget_repo.find({"quantity": 1, "name": {"$regex": f"^{tag}"}}, limit=2, offset=1)

    assert page.total == 5
    assert len(page.items) == 2


async def test_health_check_against_real_mongodb(mongo_adapter: MongoDBAdapter) -> None:
    result = await mongo_adapter.health_check()

    assert str(result.status) == "healthy"
