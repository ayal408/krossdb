"""Transparent multi-tenancy isolation: wraps any repository to auto-scope by tenant.

:class:`TenantScopedRepository` decorates an existing
:class:`~krossdb.interfaces.repository.AbstractRepository`, injecting the
ambient tenant id (see :func:`krossdb.core.context.tenant_scope`) into every
read filter and validating it on every entity read/write, so application
code never has to remember to filter by tenant manually and can't
accidentally leak another tenant's data through a forgotten filter.

Requires the wrapped model to declare a ``tenant_id: str | None`` field (as
:class:`krossdb.models.base.Entity` does).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from krossdb.core.context import get_tenant_id
from krossdb.core.exceptions import CrossTenantAccessError
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


def _reject_tenant_id_reassignment(changes: Mapping[str, Any], tenant_id: str | None) -> None:
    """Raise if ``changes`` attempts to move a record to a different (or no) tenant.

    Without this, ``update``/``bulk_update`` would forward an attacker-supplied
    ``tenant_id`` straight to the inner repository, letting the active tenant
    silently reassign one of its own records to another tenant (or detach it
    from tenancy entirely) despite every read path being tenant-scoped.
    """

    if "tenant_id" in changes and changes["tenant_id"] != tenant_id:
        raise CrossTenantAccessError(
            f"Cannot reassign tenant_id to {changes['tenant_id']!r} via update; "
            f"the active tenant context is {tenant_id!r}"
        )


class TenantScopedRepository(AbstractRepository[TModel, TId]):
    """Wraps ``inner`` so every operation is transparently scoped to the current tenant.

    Requires an active tenant context (raises ``TenantContextMissingError``
    via ``get_tenant_id(required=True)`` otherwise — see ``tenant_scope``).
    """

    def __init__(self, inner: AbstractRepository[TModel, TId]) -> None:
        self._inner = inner
        self.model = inner.model

    def _scoped_criteria(
        self, criteria: Mapping[str, Any] | Specification | None
    ) -> list[FieldFilter]:
        scoped = _normalize_criteria(criteria)
        scoped.append(
            FieldFilter(field="tenant_id", operator=Operator.EQ, value=get_tenant_id(required=True))
        )
        return scoped

    def _owned(self, entity: TModel | None) -> TModel | None:
        """Returns ``entity`` if it belongs to the active tenant, else ``None``.

        Cross-tenant reads are treated as "not found" rather than a raised
        error, so callers can't accidentally leak *whether* a record exists
        under another tenant through error-handling branches.
        """

        if entity is None:
            return None
        if getattr(entity, "tenant_id", None) != get_tenant_id(required=True):
            return None
        return entity

    async def get_by_id(self, id_: TId) -> TModel | None:
        return self._owned(await self._inner.get_by_id(id_))

    async def create(self, entity: TModel, *, idempotency_key: str | None = None) -> TModel:
        tenant_id = get_tenant_id(required=True)
        existing_tenant = getattr(entity, "tenant_id", None)
        if existing_tenant not in (None, tenant_id):
            raise CrossTenantAccessError(
                f"Cannot create an entity pre-stamped with tenant_id={existing_tenant!r} "
                f"while the active tenant context is {tenant_id!r}"
            )
        stamped = entity.model_copy(update={"tenant_id": tenant_id})
        return await self._inner.create(stamped, idempotency_key=idempotency_key)

    async def update(self, id_: TId, changes: Mapping[str, Any]) -> TModel:
        await self.get_by_id_or_raise(id_)  # raises RecordNotFoundError if not owned by this tenant
        _reject_tenant_id_reassignment(changes, get_tenant_id(required=True))
        return await self._inner.update(id_, changes)

    async def delete(self, id_: TId) -> bool:
        if await self.get_by_id(id_) is None:
            return False
        return await self._inner.delete(id_)

    async def find(
        self,
        criteria: Mapping[str, Any] | Specification | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
        sort: Sequence[SortSpec] | None = None,
    ) -> Page[TModel]:
        return await self._inner.find(
            self._scoped_criteria(criteria), limit=limit, offset=offset, sort=sort
        )

    async def count(self, criteria: Mapping[str, Any] | Specification | None = None) -> int:
        return await self._inner.count(self._scoped_criteria(criteria))

    async def bulk_create(
        self, entities: Sequence[TModel], *, idempotency_key: str | None = None
    ) -> BulkResult[TModel]:
        tenant_id = get_tenant_id(required=True)
        for entity in entities:
            existing_tenant = getattr(entity, "tenant_id", None)
            if existing_tenant not in (None, tenant_id):
                raise CrossTenantAccessError(
                    f"Cannot create an entity pre-stamped with tenant_id={existing_tenant!r} "
                    f"while the active tenant context is {tenant_id!r}"
                )
        stamped = [e.model_copy(update={"tenant_id": tenant_id}) for e in entities]
        return await self._inner.bulk_create(stamped, idempotency_key=idempotency_key)

    async def bulk_update(self, updates: Mapping[TId, Mapping[str, Any]]) -> BulkResult[TModel]:
        tenant_id = get_tenant_id(required=True)
        for changes in updates.values():
            _reject_tenant_id_reassignment(changes, tenant_id)

        owned: dict[TId, Mapping[str, Any]] = {}
        for id_, changes in updates.items():
            if await self.get_by_id(id_) is not None:
                owned[id_] = changes
        return await self._inner.bulk_update(owned)

    async def bulk_delete(self, ids: Sequence[TId]) -> int:
        owned_ids = [id_ for id_ in ids if await self.get_by_id(id_) is not None]
        return await self._inner.bulk_delete(owned_ids)


__all__ = ["TenantScopedRepository"]
