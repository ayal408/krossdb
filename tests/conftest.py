"""Shared fixtures for the whole test suite (unit + integration)."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from krossdb.core.logging import configure_logging
from krossdb.interfaces.repository import (
    AbstractRepository,
    BulkResult,
    Page,
    SortSpec,
    Specification,
    _matches_criteria,
)
from krossdb.models.base import Entity


def pytest_configure(config: pytest.Config) -> None:
    configure_logging(json_output=False)


class Widget(Entity):
    """Minimal domain entity used across unit tests."""

    name: str
    quantity: int = 0


class InMemoryWidgetRepository(AbstractRepository[Widget, uuid.UUID]):
    """A trivial in-memory implementation of the repository contract.

    Exists purely so unit tests can exercise the ABC's default behavior
    (``get_by_id_or_raise``, ``exists``) and so it can double as a reference
    implementation when writing new backend-specific repositories.
    """

    model = Widget

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, Widget] = {}

    async def get_by_id(self, id_: uuid.UUID) -> Widget | None:
        return self._store.get(id_)

    async def create(self, entity: Widget, *, idempotency_key: str | None = None) -> Widget:
        self._store[entity.id] = entity
        return entity

    async def update(
        self,
        id_: uuid.UUID,
        changes: Mapping[str, Any],
        *,
        extra_criteria: Specification | None = None,
    ) -> Widget:
        from krossdb.core.exceptions import RecordNotFoundError

        current = self._store.get(id_)
        # Indistinguishable from "id doesn't exist" by design: mirrors every
        # real backend adapter, which can't (and shouldn't) tell a caller
        # whether a row exists under another tenant.
        if current is None or (extra_criteria and not _matches_criteria(current, extra_criteria)):
            raise RecordNotFoundError("Widget", id_)
        updated = current.model_copy(update=dict(changes))
        self._store[id_] = updated
        return updated

    async def delete(self, id_: uuid.UUID, *, extra_criteria: Specification | None = None) -> bool:
        current = self._store.get(id_)
        if current is None or (extra_criteria and not _matches_criteria(current, extra_criteria)):
            return False
        del self._store[id_]
        return True

    async def find(
        self,
        criteria: Mapping[str, Any] | Specification | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[SortSpec] | None = None,
    ) -> Page[Widget]:
        items = [w for w in self._store.values() if _matches_criteria(w, criteria)]
        total = len(items)
        return Page(items=items[offset : offset + limit], total=total, limit=limit, offset=offset)

    async def count(self, criteria: Mapping[str, Any] | Specification | None = None) -> int:
        page = await self.find(criteria, limit=len(self._store) or 1)
        return page.total or 0

    async def bulk_create(
        self, entities: Sequence[Widget], *, idempotency_key: str | None = None
    ) -> BulkResult[Widget]:
        succeeded = []
        for entity in entities:
            self._store[entity.id] = entity
            succeeded.append(entity)
        return BulkResult(succeeded=succeeded)

    async def bulk_update(
        self,
        updates: Mapping[uuid.UUID, Mapping[str, Any]],
        *,
        extra_criteria: Specification | None = None,
    ) -> BulkResult[Widget]:
        succeeded, failed = [], {}
        for idx, (id_, changes) in enumerate(updates.items()):
            try:
                succeeded.append(await self.update(id_, changes, extra_criteria=extra_criteria))
            except Exception as exc:
                failed[idx] = str(exc)
        return BulkResult(succeeded=succeeded, failed=failed)

    async def bulk_delete(
        self, ids: Sequence[uuid.UUID], *, extra_criteria: Specification | None = None
    ) -> int:
        deleted = 0
        for id_ in ids:
            if await self.delete(id_, extra_criteria=extra_criteria):
                deleted += 1
        return deleted


@pytest.fixture
def widget_repository() -> InMemoryWidgetRepository:
    return InMemoryWidgetRepository()


@pytest.fixture
def make_widget():
    def _make(**overrides: Any) -> Widget:
        defaults: dict[str, Any] = {"name": "test-widget", "quantity": 1}
        defaults.update(overrides)
        return Widget(**defaults)

    return _make
