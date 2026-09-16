"""Unit tests for MongoDBRepository, with the Motor client mocked via pytest-mock.

Verifies the repository builds the correct filter/update documents and
translates results back into the Pydantic model, without touching a real
MongoDB server (see tests/integration/test_mongodb_container.py for that).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from krossdb.core.exceptions import RecordNotFoundError, UnsupportedOperationError
from krossdb.interfaces.repository import FieldFilter, Operator
from krossdb.repositories.document_repository import MongoDBRepository
from tests.conftest import Widget

pytestmark = pytest.mark.unit


class FakeCursor:
    """Mimics the chainable Motor cursor (``find().skip().limit().sort()``), async-iterable."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def skip(self, n: int) -> FakeCursor:
        return FakeCursor(self._docs[n:])

    def limit(self, n: int) -> FakeCursor:
        return FakeCursor(self._docs[:n])

    def sort(self, spec: Any) -> FakeCursor:
        return self

    def __aiter__(self):
        return self._aiter()

    async def _aiter(self):
        for doc in self._docs:
            yield doc


@pytest.fixture
def mock_collection() -> MagicMock:
    collection = MagicMock()
    collection.find_one = AsyncMock(return_value=None)
    collection.insert_one = AsyncMock()
    collection.update_one = AsyncMock(return_value=SimpleNamespace(matched_count=1))
    collection.delete_one = AsyncMock(return_value=SimpleNamespace(deleted_count=1))
    collection.delete_many = AsyncMock(return_value=SimpleNamespace(deleted_count=0))
    collection.count_documents = AsyncMock(return_value=0)
    collection.find = MagicMock(return_value=FakeCursor([]))
    return collection


@pytest.fixture
def mock_adapter(mock_collection: MagicMock) -> MagicMock:
    db = MagicMock()
    db.__getitem__.return_value = mock_collection

    acquire_cm = MagicMock()
    acquire_cm.__aenter__ = AsyncMock(return_value=db)
    acquire_cm.__aexit__ = AsyncMock(return_value=False)

    adapter = MagicMock()
    adapter.acquire.return_value = acquire_cm
    return adapter


@pytest.fixture
def repo(mock_adapter: MagicMock) -> MongoDBRepository:
    return MongoDBRepository(mock_adapter, "widgets", Widget)


async def test_get_by_id_queries_by_string_id_field(repo, mock_collection, make_widget):
    existing = make_widget(name="alpha")
    mock_collection.find_one.return_value = existing.model_dump(mode="json")

    fetched = await repo.get_by_id(existing.id)

    mock_collection.find_one.assert_awaited_once_with({"id": str(existing.id)})
    assert fetched == existing


async def test_get_by_id_returns_none_on_miss(repo, mock_collection):
    mock_collection.find_one.return_value = None

    assert await repo.get_by_id("nonexistent") is None


async def test_create_inserts_the_serialized_document(repo, mock_collection, make_widget):
    widget = make_widget(name="alpha")

    result = await repo.create(widget)

    mock_collection.insert_one.assert_awaited_once()
    (inserted_doc,), _ = mock_collection.insert_one.call_args
    assert inserted_doc["name"] == "alpha"
    assert result == widget


async def test_update_applies_set_and_refetches(repo, mock_collection, make_widget):
    existing = make_widget(name="before")
    updated_doc = existing.model_copy(update={"name": "after"}).model_dump(mode="json")
    mock_collection.find_one.return_value = updated_doc

    updated = await repo.update(existing.id, {"name": "after"})

    mock_collection.update_one.assert_awaited_once_with(
        {"id": str(existing.id)}, {"$set": {"name": "after"}}
    )
    assert updated.name == "after"


async def test_update_missing_document_raises_record_not_found(repo, mock_collection):
    mock_collection.update_one.return_value = SimpleNamespace(matched_count=0)

    with pytest.raises(RecordNotFoundError):
        await repo.update("missing-id", {"name": "x"})


async def test_delete_returns_true_when_a_document_was_removed(repo, mock_collection):
    mock_collection.delete_one.return_value = SimpleNamespace(deleted_count=1)

    assert await repo.delete("some-id") is True


async def test_delete_returns_false_when_nothing_matched(repo, mock_collection):
    mock_collection.delete_one.return_value = SimpleNamespace(deleted_count=0)

    assert await repo.delete("some-id") is False


async def test_find_returns_page_with_total_from_count_documents(
    repo, mock_collection, make_widget
):
    docs = [make_widget(name=f"w{i}").model_dump(mode="json") for i in range(3)]
    mock_collection.count_documents.return_value = 3
    mock_collection.find.return_value = FakeCursor(docs)

    page = await repo.find(limit=3, offset=0)

    assert page.total == 3
    assert len(page.items) == 3


async def test_count_delegates_to_count_documents_with_criteria(repo, mock_collection):
    mock_collection.count_documents.return_value = 7

    total = await repo.count({"name": "alpha"})

    mock_collection.count_documents.assert_awaited_once_with({"name": "alpha"})
    assert total == 7


async def test_count_with_plain_dict_criteria_is_still_supported(repo, mock_collection):
    mock_collection.count_documents.return_value = 7

    total = await repo.count({"name": "alpha"})

    mock_collection.count_documents.assert_awaited_once_with({"name": "alpha"})
    assert total == 7


async def test_find_with_ne_operator_builds_ne_query(repo, mock_collection):
    mock_collection.count_documents.return_value = 0

    await repo.find([FieldFilter(field="quantity", operator=Operator.NE, value=1)])

    mock_collection.count_documents.assert_awaited_once_with({"quantity": {"$ne": 1}})


async def test_find_with_gt_and_lte_operators_builds_range_query(repo, mock_collection):
    mock_collection.count_documents.return_value = 0

    await repo.find(
        [
            FieldFilter(field="quantity", operator=Operator.GT, value=1),
            FieldFilter(field="quantity", operator=Operator.LTE, value=5),
        ]
    )

    mock_collection.count_documents.assert_awaited_once_with({"quantity": {"$gt": 1, "$lte": 5}})


async def test_find_with_gte_and_lt_operators_builds_range_query(repo, mock_collection):
    mock_collection.count_documents.return_value = 0

    await repo.find([FieldFilter(field="quantity", operator=Operator.GTE, value=2)])

    mock_collection.count_documents.assert_awaited_once_with({"quantity": {"$gte": 2}})

    mock_collection.count_documents.reset_mock()
    await repo.find([FieldFilter(field="quantity", operator=Operator.LT, value=2)])

    mock_collection.count_documents.assert_awaited_once_with({"quantity": {"$lt": 2}})


async def test_find_with_in_and_not_in_operators(repo, mock_collection):
    mock_collection.count_documents.return_value = 0

    await repo.find([FieldFilter(field="quantity", operator=Operator.IN, value=[1, 2])])

    mock_collection.count_documents.assert_awaited_once_with({"quantity": {"$in": [1, 2]}})

    mock_collection.count_documents.reset_mock()
    await repo.find([FieldFilter(field="quantity", operator=Operator.NOT_IN, value=[1, 2])])

    mock_collection.count_documents.assert_awaited_once_with({"quantity": {"$nin": [1, 2]}})


async def test_find_with_contains_operator_builds_regex_query(repo, mock_collection):
    mock_collection.count_documents.return_value = 0

    await repo.find([FieldFilter(field="name", operator=Operator.CONTAINS, value="al.pha")])

    mock_collection.count_documents.assert_awaited_once_with({"name": {"$regex": "al\\.pha"}})


async def test_find_with_is_null_operator(repo, mock_collection):
    mock_collection.count_documents.return_value = 0

    await repo.find([FieldFilter(field="tenant_id", operator=Operator.IS_NULL, value=True)])

    mock_collection.count_documents.assert_awaited_once_with({"tenant_id": None})

    mock_collection.count_documents.reset_mock()
    await repo.find([FieldFilter(field="tenant_id", operator=Operator.IS_NULL, value=False)])

    mock_collection.count_documents.assert_awaited_once_with({"tenant_id": {"$ne": None}})


async def test_find_raises_for_unsupported_operator(repo, mock_collection):
    bogus_filter = FieldFilter.model_construct(field="quantity", operator="bogus", value=1)

    with pytest.raises(UnsupportedOperationError):
        await repo.find([bogus_filter])


async def test_bulk_create_inserts_each_entity_and_reports_all_succeeded(
    repo, mock_collection, make_widget
):
    widgets = [make_widget(name=f"w{i}") for i in range(3)]

    result = await repo.bulk_create(widgets)

    assert mock_collection.insert_one.await_count == 3
    assert result.all_succeeded
    assert result.success_count == 3


async def test_bulk_delete_uses_in_query_and_returns_deleted_count(
    repo, mock_collection, make_widget
):
    ids = [make_widget().id for _ in range(3)]
    mock_collection.delete_many.return_value = SimpleNamespace(deleted_count=3)

    deleted = await repo.bulk_delete(ids)

    mock_collection.delete_many.assert_awaited_once_with({"id": {"$in": [str(i) for i in ids]}})
    assert deleted == 3


async def test_bulk_delete_with_empty_ids_short_circuits(repo, mock_collection):
    deleted = await repo.bulk_delete([])

    mock_collection.delete_many.assert_not_awaited()
    assert deleted == 0
