"""Integration tests against a real Qdrant container (via Testcontainers).

A raw-client smoke test proves the fixture works; the rest exercise
``QdrantAdapter`` + ``QdrantRepository`` (including similarity search) end
to end against the real server.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("qdrant_client")

from nexusdb.adapters.vector.qdrant import QdrantAdapter
from nexusdb.core.config import ConnectionConfig, NodeConfig
from nexusdb.core.enums import DatabaseKind, RoutingRole
from nexusdb.core.exceptions import RecordNotFoundError
from nexusdb.models.base import Entity
from nexusdb.repositories.vector_repository import QdrantRepository

_COLLECTION = "widgets_it"


class EmbeddedWidget(Entity):
    name: str
    vector: list[float]


async def test_can_create_collection_and_upsert_and_search(qdrant_url: str) -> None:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    client = AsyncQdrantClient(url=qdrant_url, check_compatibility=False)
    try:
        await client.create_collection(
            collection_name="raw_widgets_it",
            vectors_config=VectorParams(size=4, distance=Distance.COSINE),
        )
        await client.upsert(
            collection_name="raw_widgets_it",
            points=[PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={"name": "widget-a"})],
        )

        count = await client.count("raw_widgets_it")
        assert count.count == 1

        hits = await client.query_points(
            collection_name="raw_widgets_it", query=[0.1, 0.2, 0.3, 0.4], limit=1
        )
        assert hits.points[0].payload["name"] == "widget-a"
    finally:
        await client.close()


@pytest_asyncio.fixture
async def qdrant_adapter(qdrant_url: str) -> AsyncIterator[QdrantAdapter]:
    config = ConnectionConfig(
        name="qdrant-it",
        kind=DatabaseKind.QDRANT,
        nodes=[NodeConfig(dsn=qdrant_url, role=RoutingRole.MASTER)],
    )
    adapter = QdrantAdapter(config)
    await adapter.connect()

    from qdrant_client.models import Distance, VectorParams

    async with adapter.acquire() as client:
        if not await client.collection_exists(_COLLECTION):
            await client.create_collection(
                collection_name=_COLLECTION,
                vectors_config=VectorParams(size=4, distance=Distance.COSINE),
            )

    yield adapter
    await adapter.disconnect()


@pytest.fixture
def widget_repo(qdrant_adapter: QdrantAdapter) -> QdrantRepository:
    return QdrantRepository(qdrant_adapter, _COLLECTION, EmbeddedWidget)


def _entity(**overrides) -> EmbeddedWidget:
    defaults = {"name": f"widget-{uuid.uuid4()}", "vector": [0.1, 0.2, 0.3, 0.4]}
    defaults.update(overrides)
    return EmbeddedWidget(**defaults)


async def test_repository_crud_round_trip(widget_repo: QdrantRepository) -> None:
    entity = _entity()

    await widget_repo.create(entity)
    fetched = await widget_repo.get_by_id(entity.id)
    assert fetched is not None and fetched.name == entity.name

    updated = await widget_repo.update(entity.id, {"name": "renamed"})
    assert updated.name == "renamed"

    assert await widget_repo.delete(entity.id) is True
    assert await widget_repo.get_by_id(entity.id) is None


async def test_repository_update_missing_raises_record_not_found(
    widget_repo: QdrantRepository,
) -> None:
    with pytest.raises(RecordNotFoundError):
        await widget_repo.update(uuid.uuid4(), {"name": "x"})


async def test_bulk_create_and_bulk_delete(widget_repo: QdrantRepository) -> None:
    entities = [_entity() for _ in range(3)]

    result = await widget_repo.bulk_create(entities)
    assert result.all_succeeded

    deleted = await widget_repo.bulk_delete([e.id for e in entities])
    assert deleted == 3


async def test_search_returns_the_closest_match_first(widget_repo: QdrantRepository) -> None:
    close = _entity(name="closest", vector=[1.0, 0.0, 0.0, 0.0])
    far = _entity(name="farthest", vector=[0.0, 1.0, 0.0, 0.0])
    await widget_repo.bulk_create([close, far])

    hits = await widget_repo.search([1.0, 0.0, 0.0, 0.0], limit=2)

    assert hits[0][0].name == "closest"


async def test_health_check_against_real_qdrant(qdrant_adapter: QdrantAdapter) -> None:
    result = await qdrant_adapter.health_check()

    assert str(result.status) == "healthy"
