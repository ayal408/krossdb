"""Generic repository over a Qdrant collection, backed by :class:`QdrantAdapter`.

Works with any Pydantic model that has an ``id`` field and a ``vector:
list[float]`` field; every other field becomes point payload. Beyond the
standard :class:`AbstractRepository` CRUD contract, this repository adds
:meth:`search` for similarity search, which is specific to vector stores and
has no equivalent in the relational/document repositories.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from qdrant_client.models import (
    FieldCondition,
    Filter,
    IsNullCondition,
    MatchAny,
    MatchExcept,
    MatchText,
    MatchValue,
    PayloadField,
    PointStruct,
    Range,
)

from krossdb.adapters.vector.qdrant import QdrantAdapter
from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import KrossDBError, RecordNotFoundError, UnsupportedOperationError
from krossdb.interfaces.repository import (
    AbstractRepository,
    BulkResult,
    FieldFilter,
    Operator,
    Page,
    SortSpec,
    Specification,
    TId,
    TModel,
    _normalize_criteria,
)


def _criteria_to_filter(criteria: Mapping[str, Any] | Specification | None) -> Filter | None:
    filters = _normalize_criteria(criteria)
    if not filters:
        return None
    must: list[Any] = []
    must_not: list[Any] = []
    for filt in filters:
        _add_condition(filt, must, must_not)
    return Filter(must=must or None, must_not=must_not or None)


def _add_condition(filt: FieldFilter, must: list[Any], must_not: list[Any]) -> None:
    operator = filt.operator
    value = filt.value
    if operator is Operator.EQ:
        must.append(FieldCondition(key=filt.field, match=MatchValue(value=value)))
    elif operator is Operator.NE:
        # MatchExcept's field is named `except_` in Python but aliased to the
        # reserved word `except` on the wire, so it must be constructed via
        # keyword-unpacking rather than a literal `except_=` argument.
        must.append(FieldCondition(key=filt.field, match=MatchExcept(**{"except": [value]})))
    elif operator is Operator.IN:
        must.append(FieldCondition(key=filt.field, match=MatchAny(any=list(value))))
    elif operator is Operator.NOT_IN:
        must_not.append(FieldCondition(key=filt.field, match=MatchAny(any=list(value))))
    elif operator is Operator.GT:
        must.append(FieldCondition(key=filt.field, range=Range(gt=value)))
    elif operator is Operator.GTE:
        must.append(FieldCondition(key=filt.field, range=Range(gte=value)))
    elif operator is Operator.LT:
        must.append(FieldCondition(key=filt.field, range=Range(lt=value)))
    elif operator is Operator.LTE:
        must.append(FieldCondition(key=filt.field, range=Range(lte=value)))
    elif operator is Operator.CONTAINS:
        # MatchText tokenizes and requires a full-text index on the field; it is
        # the closest Qdrant equivalent to a substring filter.
        must.append(FieldCondition(key=filt.field, match=MatchText(text=str(value))))
    elif operator is Operator.IS_NULL:
        condition = IsNullCondition(is_null=PayloadField(key=filt.field))
        (must if value else must_not).append(condition)
    else:
        raise UnsupportedOperationError(
            f"Operator {operator!r} is not supported by the vector adapter"
        )


class QdrantRepository(AbstractRepository[TModel, TId]):
    def __init__(self, adapter: QdrantAdapter, collection_name: str, model: type[TModel]) -> None:
        self.adapter = adapter
        self.collection_name = collection_name
        self.model = model

    def _entity_to_point(self, entity: TModel) -> PointStruct:
        data = entity.model_dump(mode="json")
        vector = data.pop("vector")
        point_id = data.pop("id")
        return PointStruct(id=point_id, vector=vector, payload=data)

    def _point_to_model(self, point: Any) -> TModel:
        payload = dict(point.payload or {})
        payload["id"] = point.id
        payload["vector"] = getattr(point, "vector", None) or []
        return self.model.model_validate(payload)

    async def get_by_id(self, id_: TId) -> TModel | None:
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="get_by_id"):
                points = await client.retrieve(
                    self.collection_name, ids=[str(id_)], with_vectors=True
                )
        return self._point_to_model(points[0]) if points else None

    async def create(self, entity: TModel, *, idempotency_key: str | None = None) -> TModel:
        point = self._entity_to_point(entity)
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="create"):
                await client.upsert(self.collection_name, points=[point])
        return entity

    async def update(self, id_: TId, changes: Mapping[str, Any]) -> TModel:
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="update"):
                await client.set_payload(
                    self.collection_name, payload=dict(changes), points=[str(id_)]
                )
        updated = await self.get_by_id(id_)
        if updated is None:
            raise RecordNotFoundError(self.model.__name__, id_)
        return updated

    async def delete(self, id_: TId) -> bool:
        existing = await self.get_by_id(id_)
        if existing is None:
            return False
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="delete"):
                await client.delete(self.collection_name, points_selector=[str(id_)])
        return True

    async def find(
        self,
        criteria: Mapping[str, Any] | Specification | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[SortSpec] | None = None,
    ) -> Page[TModel]:
        query_filter = _criteria_to_filter(criteria)
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="find"):
                points, _ = await client.scroll(
                    self.collection_name,
                    scroll_filter=query_filter,
                    limit=limit,
                    offset=offset,
                    with_vectors=True,
                )
                total = await client.count(self.collection_name, count_filter=query_filter)
        return Page(
            items=[self._point_to_model(p) for p in points],
            total=total.count,
            limit=limit,
            offset=offset,
        )

    async def count(self, criteria: Mapping[str, Any] | Specification | None = None) -> int:
        query_filter = _criteria_to_filter(criteria)
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="count"):
                result = await client.count(self.collection_name, count_filter=query_filter)
        return result.count

    async def bulk_create(
        self, entities: Sequence[TModel], *, idempotency_key: str | None = None
    ) -> BulkResult[TModel]:
        points = [self._entity_to_point(e) for e in entities]
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="bulk_create"):
                await client.upsert(self.collection_name, points=points)
        return BulkResult(succeeded=list(entities))

    async def bulk_update(self, updates: Mapping[TId, Mapping[str, Any]]) -> BulkResult[TModel]:
        succeeded: list[TModel] = []
        failed: dict[int, str] = {}
        for index, (id_, changes) in enumerate(updates.items()):
            try:
                succeeded.append(await self.update(id_, changes))
            except KrossDBError as exc:
                failed[index] = str(exc)
        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_delete(self, ids: Sequence[TId]) -> int:
        if not ids:
            return 0
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="bulk_delete"):
                await client.delete(self.collection_name, points_selector=[str(i) for i in ids])
        return len(ids)  # Qdrant's delete doesn't report how many points actually existed

    async def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 10,
        criteria: Mapping[str, Any] | Specification | None = None,
    ) -> list[tuple[TModel, float]]:
        """Similarity search: returns ``(entity, score)`` pairs ordered by relevance."""

        query_filter = _criteria_to_filter(criteria)
        async with self.adapter.acquire() as client:
            with translate_exceptions(collection=self.collection_name, op="search"):
                result = await client.query_points(
                    self.collection_name,
                    query=list(vector),
                    limit=limit,
                    query_filter=query_filter,
                    with_vectors=True,
                )
        return [(self._point_to_model(p), p.score) for p in result.points]


__all__ = ["QdrantRepository"]
