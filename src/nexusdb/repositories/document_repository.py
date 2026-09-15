"""Generic repository over a MongoDB collection, backed by :class:`MongoDBAdapter`.

Every entity's ``id`` field is stored verbatim (via Pydantic JSON-mode
serialization, so a UUID becomes its string form) as a regular indexed field
rather than overloading Mongo's special ``_id``, so id types stay consistent
across relational/document/vector backends. Callers are expected to create a
unique index on the id field themselves (e.g. via the CLI's schema command or
their own migration).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from pymongo import ASCENDING, DESCENDING

from nexusdb.adapters.document.mongodb import MongoDBAdapter
from nexusdb.core.exception_mapper import translate_exceptions
from nexusdb.core.exceptions import NexusDBError, RecordNotFoundError, UnsupportedOperationError
from nexusdb.interfaces.repository import (
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

_MONGO_OPERATORS: dict[Operator, str] = {
    Operator.NE: "$ne",
    Operator.GT: "$gt",
    Operator.GTE: "$gte",
    Operator.LT: "$lt",
    Operator.LTE: "$lte",
    Operator.IN: "$in",
    Operator.NOT_IN: "$nin",
}


def _clause_for_filter(filt: FieldFilter) -> Any:
    operator = filt.operator
    value = filt.value
    if operator is Operator.EQ:
        return value
    if operator is Operator.IS_NULL:
        # `{field: None}` already matches a missing field too, which is the
        # usual meaning of "is null"; `$ne: None` excludes both missing and null.
        return None if value else {"$ne": None}
    if operator is Operator.CONTAINS:
        return {"$regex": re.escape(str(value))}
    if operator in _MONGO_OPERATORS:
        return {_MONGO_OPERATORS[operator]: value}
    raise UnsupportedOperationError(
        f"Operator {operator!r} is not supported by the document adapter"
    )


def _criteria_to_query(criteria: Mapping[str, Any] | Specification | None) -> dict[str, Any]:
    query: dict[str, Any] = {}
    extra_clauses: list[dict[str, Any]] = []
    for filt in _normalize_criteria(criteria):
        clause = _clause_for_filter(filt)
        existing = query.get(filt.field)
        if filt.field not in query:
            query[filt.field] = clause
        elif (
            isinstance(existing, dict)
            and isinstance(clause, dict)
            and existing.keys().isdisjoint(clause)
        ):
            # Merge only when the operator keys don't overlap; an overlap (or
            # a non-mergeable scalar clause) would otherwise silently
            # overwrite the earlier constraint instead of AND-ing it in.
            query[filt.field] = {**existing, **clause}
        else:
            extra_clauses.append({filt.field: clause})
    if extra_clauses:
        return {"$and": [query, *extra_clauses]} if query else {"$and": extra_clauses}
    return query


class MongoDBRepository(AbstractRepository[TModel, TId]):
    def __init__(
        self,
        adapter: MongoDBAdapter,
        collection_name: str,
        model: type[TModel],
        *,
        id_field: str = "id",
    ) -> None:
        self.adapter = adapter
        self.collection_name = collection_name
        self.model = model
        self.id_field = id_field

    def _doc_to_model(self, doc: Mapping[str, Any]) -> TModel:
        clean = dict(doc)
        clean.pop("_id", None)
        return self.model.model_validate(clean)

    def _entity_to_doc(self, entity: TModel) -> dict[str, Any]:
        return entity.model_dump(mode="json")

    async def get_by_id(self, id_: TId) -> TModel | None:
        async with self.adapter.acquire() as db:
            with translate_exceptions(collection=self.collection_name, op="get_by_id"):
                doc = await db[self.collection_name].find_one({self.id_field: str(id_)})
        return self._doc_to_model(doc) if doc else None

    async def create(self, entity: TModel, *, idempotency_key: str | None = None) -> TModel:
        async with self.adapter.acquire() as db:
            with translate_exceptions(collection=self.collection_name, op="create"):
                await db[self.collection_name].insert_one(self._entity_to_doc(entity))
        return entity

    async def update(self, id_: TId, changes: Mapping[str, Any]) -> TModel:
        async with self.adapter.acquire() as db:
            with translate_exceptions(collection=self.collection_name, op="update"):
                result = await db[self.collection_name].update_one(
                    {self.id_field: str(id_)}, {"$set": dict(changes)}
                )
        if result.matched_count == 0:
            raise RecordNotFoundError(self.model.__name__, id_)
        updated = await self.get_by_id(id_)
        assert updated is not None  # matched_count > 0 guarantees the document exists
        return updated

    async def delete(self, id_: TId) -> bool:
        async with self.adapter.acquire() as db:
            with translate_exceptions(collection=self.collection_name, op="delete"):
                result = await db[self.collection_name].delete_one({self.id_field: str(id_)})
        return result.deleted_count > 0

    async def find(
        self,
        criteria: Mapping[str, Any] | Specification | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[SortSpec] | None = None,
    ) -> Page[TModel]:
        query = _criteria_to_query(criteria)
        async with self.adapter.acquire() as db:
            collection = db[self.collection_name]
            with translate_exceptions(collection=self.collection_name, op="find"):
                total = await collection.count_documents(query)
                cursor = collection.find(query).skip(offset).limit(limit)
                if sort:
                    cursor = cursor.sort(
                        [(s.field, DESCENDING if s.descending else ASCENDING) for s in sort]
                    )
                docs = [doc async for doc in cursor]
        return Page(
            items=[self._doc_to_model(doc) for doc in docs], total=total, limit=limit, offset=offset
        )

    async def count(self, criteria: Mapping[str, Any] | Specification | None = None) -> int:
        query = _criteria_to_query(criteria)
        async with self.adapter.acquire() as db:
            with translate_exceptions(collection=self.collection_name, op="count"):
                return await db[self.collection_name].count_documents(query)

    async def bulk_create(
        self, entities: Sequence[TModel], *, idempotency_key: str | None = None
    ) -> BulkResult[TModel]:
        succeeded: list[TModel] = []
        failed: dict[int, str] = {}
        async with self.adapter.acquire() as db:
            collection = db[self.collection_name]
            for index, entity in enumerate(entities):
                try:
                    with translate_exceptions(collection=self.collection_name, op="bulk_create"):
                        await collection.insert_one(self._entity_to_doc(entity))
                    succeeded.append(entity)
                except NexusDBError as exc:
                    failed[index] = str(exc)
        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_update(self, updates: Mapping[TId, Mapping[str, Any]]) -> BulkResult[TModel]:
        succeeded: list[TModel] = []
        failed: dict[int, str] = {}
        for index, (id_, changes) in enumerate(updates.items()):
            try:
                succeeded.append(await self.update(id_, changes))
            except NexusDBError as exc:
                failed[index] = str(exc)
        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_delete(self, ids: Sequence[TId]) -> int:
        if not ids:
            return 0
        async with self.adapter.acquire() as db:
            with translate_exceptions(collection=self.collection_name, op="bulk_delete"):
                result = await db[self.collection_name].delete_many(
                    {self.id_field: {"$in": [str(i) for i in ids]}}
                )
        return result.deleted_count


__all__ = ["MongoDBRepository"]
