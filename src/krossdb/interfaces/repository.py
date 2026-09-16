"""Generic repository contract shared by relational, document, and vector stores.

Concrete repositories are parameterized on a Pydantic model (the domain
entity) and an id type, giving callers full static typing:

    class UserRepository(AbstractRepository[User, UUID]): ...

Every mutating method accepts an optional ``idempotency_key``; adapters that
can enforce it natively (e.g. via a unique constraint on the key) should, and
the Unit of Work provides a generic fallback (see
:mod:`krossdb.interfaces.unit_of_work`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Generic, TypeAlias, TypeVar

from pydantic import BaseModel, ConfigDict, Field

TModel = TypeVar("TModel", bound=BaseModel)
TId = TypeVar("TId")


class SortSpec(BaseModel):
    """One field to sort by; repositories combine a list of these for multi-key sort."""

    model_config = ConfigDict(frozen=True)

    field: str
    descending: bool = False


class Operator(StrEnum):
    """Comparison operators a :class:`FieldFilter` can express.

    Adapters translate each member to their own query language; a backend
    that cannot express a given operator raises ``UnsupportedOperationError``
    rather than silently degrading to a different comparison.
    """

    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in_"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    IS_NULL = "is_null"


class FieldFilter(BaseModel):
    """One ``field OP value`` comparison; a :class:`Specification` ANDs a sequence of these."""

    model_config = ConfigDict(frozen=True)

    field: str
    operator: Operator = Operator.EQ
    value: Any = None


Specification: TypeAlias = Sequence[FieldFilter]
"""An implicit AND of :class:`FieldFilter`; the richer alternative to a plain equality mapping."""


def _normalize_criteria(criteria: Mapping[str, Any] | Specification | None) -> list[FieldFilter]:
    """Turn either accepted ``criteria`` shape into a uniform list adapters can translate.

    A plain mapping is sugar for a list of ``eq`` filters, kept for backward compatibility
    with callers written before :class:`Specification` existed.
    """

    if criteria is None:
        return []
    if isinstance(criteria, Mapping):
        return [
            FieldFilter(field=key, operator=Operator.EQ, value=value)
            for key, value in criteria.items()
        ]
    return list(criteria)


class Page(BaseModel, Generic[TModel]):
    """A page of results plus enough metadata to fetch the next one."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[TModel]
    total: int | None = Field(
        default=None, description="Total matches, if the backend computed it cheaply"
    )
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.total is not None and self.offset + len(self.items) < self.total


class BulkResult(BaseModel, Generic[TModel]):
    """Outcome of a bulk operation, supporting partial success."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    succeeded: list[TModel] = Field(default_factory=list)
    failed: dict[int, str] = Field(default_factory=dict, description="index -> error message")

    @property
    def success_count(self) -> int:
        return len(self.succeeded)

    @property
    def failure_count(self) -> int:
        return len(self.failed)

    @property
    def all_succeeded(self) -> bool:
        return not self.failed


class AbstractRepository(ABC, Generic[TModel, TId]):
    """CRUD + bulk + query contract implemented once per (model, backend) pair."""

    model: type[TModel]

    # -- single-item CRUD ----------------------------------------------------

    @abstractmethod
    async def get_by_id(self, id_: TId) -> TModel | None:
        """Return the entity, or ``None`` if it does not exist (never raises for a miss)."""

    async def get_by_id_or_raise(self, id_: TId) -> TModel:
        entity = await self.get_by_id(id_)
        if entity is None:
            from krossdb.core.exceptions import RecordNotFoundError

            raise RecordNotFoundError(self.model.__name__, id_)
        return entity

    @abstractmethod
    async def create(self, entity: TModel, *, idempotency_key: str | None = None) -> TModel:
        """Persist a new entity, returning it with any backend-assigned fields populated."""

    @abstractmethod
    async def update(self, id_: TId, changes: Mapping[str, Any]) -> TModel:
        """Apply a partial update and return the resulting entity."""

    @abstractmethod
    async def delete(self, id_: TId) -> bool:
        """Delete by id; returns ``True`` if a record was actually removed."""

    # -- querying -------------------------------------------------------------

    @abstractmethod
    async def find(
        self,
        criteria: Mapping[str, Any] | Specification | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[SortSpec] | None = None,
    ) -> Page[TModel]:
        """Query by a plain equality mapping or a richer :class:`Specification` of filters."""

    @abstractmethod
    async def count(self, criteria: Mapping[str, Any] | Specification | None = None) -> int: ...

    async def exists(self, criteria: Mapping[str, Any] | Specification | None = None) -> bool:
        return await self.count(criteria) > 0

    # -- bulk operations --------------------------------------------------------

    @abstractmethod
    async def bulk_create(
        self, entities: Sequence[TModel], *, idempotency_key: str | None = None
    ) -> BulkResult[TModel]: ...

    @abstractmethod
    async def bulk_update(self, updates: Mapping[TId, Mapping[str, Any]]) -> BulkResult[TModel]: ...

    @abstractmethod
    async def bulk_delete(self, ids: Sequence[TId]) -> int:
        """Returns the number of records actually deleted."""


__all__ = [
    "AbstractRepository",
    "BulkResult",
    "FieldFilter",
    "Operator",
    "Page",
    "Specification",
    "SortSpec",
    "TId",
    "TModel",
]
