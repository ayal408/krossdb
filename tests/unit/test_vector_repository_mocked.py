"""Unit tests for QdrantRepository, with the Qdrant client mocked via pytest-mock.

Verifies point construction/serialization and query-result translation
without touching a real Qdrant server (see
tests/integration/test_qdrant_container.py for that).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from krossdb.core.exceptions import RecordNotFoundError, UnsupportedOperationError
from krossdb.interfaces.repository import FieldFilter, Operator
from krossdb.models.base import Entity
from krossdb.repositories.vector_repository import QdrantRepository

pytestmark = pytest.mark.unit


class EmbeddedWidget(Entity):
    name: str
    vector: list[float]


def _fake_point(entity: EmbeddedWidget, score: float | None = None):
    payload = entity.model_dump(mode="json")
    payload.pop("id")
    vector = payload.pop("vector")
    point = SimpleNamespace(id=str(entity.id), payload=payload, vector=vector)
    if score is not None:
        point.score = score
    return point


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.retrieve = AsyncMock(return_value=[])
    client.upsert = AsyncMock()
    client.set_payload = AsyncMock()
    client.delete = AsyncMock()
    client.scroll = AsyncMock(return_value=([], None))
    client.count = AsyncMock(return_value=SimpleNamespace(count=0))
    client.query_points = AsyncMock(return_value=SimpleNamespace(points=[]))
    return client


@pytest.fixture
def mock_adapter(mock_client: MagicMock) -> MagicMock:
    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=mock_client)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    adapter = MagicMock()
    adapter.acquire.return_value = acquire_cm
    return adapter


@pytest.fixture
def repo(mock_adapter: MagicMock) -> QdrantRepository:
    return QdrantRepository(mock_adapter, "widgets", EmbeddedWidget)


def _make_entity(**overrides) -> EmbeddedWidget:
    defaults = {"name": "alpha", "vector": [0.1, 0.2, 0.3]}
    defaults.update(overrides)
    return EmbeddedWidget(**defaults)


async def test_create_upserts_a_point_with_id_vector_and_payload(repo, mock_client):
    entity = _make_entity()

    result = await repo.create(entity)

    mock_client.upsert.assert_awaited_once()
    _, kwargs = mock_client.upsert.call_args
    points = kwargs["points"]
    assert len(points) == 1
    assert points[0].id == str(entity.id)
    assert points[0].vector == entity.vector
    assert points[0].payload["name"] == "alpha"
    assert result == entity


async def test_get_by_id_returns_none_when_no_point_found(repo, mock_client):
    mock_client.retrieve.return_value = []

    assert await repo.get_by_id("missing") is None


async def test_get_by_id_reconstructs_the_model_from_the_point(repo, mock_client):
    entity = _make_entity(name="beta")
    mock_client.retrieve.return_value = [_fake_point(entity)]

    fetched = await repo.get_by_id(entity.id)

    assert fetched.name == "beta"
    assert fetched.vector == entity.vector


async def test_update_sets_payload_and_refetches(repo, mock_client):
    entity = _make_entity(name="before")
    updated = entity.model_copy(update={"name": "after"})
    mock_client.retrieve.return_value = [_fake_point(updated)]

    result = await repo.update(entity.id, {"name": "after"})

    mock_client.set_payload.assert_awaited_once()
    assert result.name == "after"


async def test_update_missing_point_raises_record_not_found(repo, mock_client):
    mock_client.retrieve.return_value = []

    with pytest.raises(RecordNotFoundError):
        await repo.update("missing", {"name": "x"})


async def test_delete_returns_false_when_point_does_not_exist(repo, mock_client):
    mock_client.retrieve.return_value = []

    assert await repo.delete("missing") is False
    mock_client.delete.assert_not_awaited()


async def test_delete_removes_existing_point(repo, mock_client):
    entity = _make_entity()
    mock_client.retrieve.return_value = [_fake_point(entity)]

    assert await repo.delete(entity.id) is True
    mock_client.delete.assert_awaited_once()


async def test_find_returns_page_built_from_scroll_and_count(repo, mock_client):
    entities = [_make_entity(name=f"w{i}") for i in range(2)]
    mock_client.scroll.return_value = ([_fake_point(e) for e in entities], None)
    mock_client.count.return_value = SimpleNamespace(count=2)

    page = await repo.find(limit=10)

    assert page.total == 2
    assert len(page.items) == 2


async def test_find_with_plain_dict_criteria_builds_match_value_filter(repo, mock_client):
    await repo.find(criteria={"name": "alpha"})

    _, kwargs = mock_client.scroll.call_args
    condition = kwargs["scroll_filter"].must[0]
    assert condition.key == "name"
    assert condition.match.value == "alpha"


async def test_find_with_ne_operator_builds_match_except_filter(repo, mock_client):
    await repo.find(criteria=[FieldFilter(field="name", operator=Operator.NE, value="alpha")])

    _, kwargs = mock_client.scroll.call_args
    condition = kwargs["scroll_filter"].must[0]
    assert condition.key == "name"
    assert condition.match.except_ == ["alpha"]


async def test_find_with_gt_and_lte_operators_build_range_filters(repo, mock_client):
    await repo.find(
        criteria=[
            FieldFilter(field="quantity", operator=Operator.GT, value=1),
            FieldFilter(field="quantity", operator=Operator.LTE, value=5),
        ]
    )

    _, kwargs = mock_client.scroll.call_args
    conditions = kwargs["scroll_filter"].must
    assert conditions[0].range.gt == 1
    assert conditions[1].range.lte == 5


async def test_find_with_in_operator_builds_match_any_filter(repo, mock_client):
    await repo.find(criteria=[FieldFilter(field="quantity", operator=Operator.IN, value=[1, 2])])

    _, kwargs = mock_client.scroll.call_args
    condition = kwargs["scroll_filter"].must[0]
    assert condition.match.any == [1, 2]


async def test_find_with_not_in_operator_builds_must_not_match_any_filter(repo, mock_client):
    await repo.find(
        criteria=[FieldFilter(field="quantity", operator=Operator.NOT_IN, value=[1, 2])]
    )

    _, kwargs = mock_client.scroll.call_args
    query_filter = kwargs["scroll_filter"]
    assert query_filter.must is None
    assert query_filter.must_not[0].match.any == [1, 2]


async def test_find_with_contains_operator_builds_match_text_filter(repo, mock_client):
    await repo.find(criteria=[FieldFilter(field="name", operator=Operator.CONTAINS, value="alp")])

    _, kwargs = mock_client.scroll.call_args
    condition = kwargs["scroll_filter"].must[0]
    assert condition.match.text == "alp"


async def test_find_with_is_null_operator_true_builds_must_is_null_condition(repo, mock_client):
    await repo.find(criteria=[FieldFilter(field="name", operator=Operator.IS_NULL, value=True)])

    _, kwargs = mock_client.scroll.call_args
    query_filter = kwargs["scroll_filter"]
    assert query_filter.must[0].is_null.key == "name"
    assert query_filter.must_not is None


async def test_find_with_is_null_operator_false_builds_must_not_is_null_condition(
    repo, mock_client
):
    await repo.find(criteria=[FieldFilter(field="name", operator=Operator.IS_NULL, value=False)])

    _, kwargs = mock_client.scroll.call_args
    query_filter = kwargs["scroll_filter"]
    assert query_filter.must is None
    assert query_filter.must_not[0].is_null.key == "name"


async def test_find_raises_for_unsupported_operator(repo, mock_client):
    bogus_filter = FieldFilter.model_construct(field="quantity", operator="bogus", value=1)

    with pytest.raises(UnsupportedOperationError):
        await repo.find(criteria=[bogus_filter])


async def test_bulk_create_upserts_all_points_at_once(repo, mock_client):
    entities = [_make_entity(name=f"w{i}") for i in range(3)]

    result = await repo.bulk_create(entities)

    mock_client.upsert.assert_awaited_once()
    assert result.all_succeeded
    assert result.success_count == 3


async def test_search_returns_entity_score_pairs_ordered_by_relevance(repo, mock_client):
    entity = _make_entity(name="closest")
    mock_client.query_points.return_value = SimpleNamespace(
        points=[_fake_point(entity, score=0.99)]
    )

    hits = await repo.search([0.1, 0.2, 0.3], limit=5)

    assert len(hits) == 1
    matched_entity, score = hits[0]
    assert matched_entity.name == "closest"
    assert score == 0.99


async def test_bulk_delete_with_empty_ids_short_circuits(repo, mock_client):
    deleted = await repo.bulk_delete([])

    mock_client.delete.assert_not_awaited()
    assert deleted == 0
