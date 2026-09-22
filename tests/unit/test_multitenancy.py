"""Unit tests for TenantScopedRepository, using the in-memory reference repository."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from krossdb.core.context import tenant_scope
from krossdb.core.exceptions import CrossTenantAccessError, TenantContextMissingError
from krossdb.multitenancy.context import TenantScopedRepository

pytestmark = pytest.mark.unit


@pytest.fixture
def scoped_repository(widget_repository):
    return TenantScopedRepository(widget_repository)


async def test_create_without_tenant_context_raises(scoped_repository, make_widget):
    with pytest.raises(TenantContextMissingError):
        await scoped_repository.create(make_widget())


async def test_create_stamps_the_active_tenant(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="alpha"))

    assert created.tenant_id == "tenant-a"


async def test_create_rejects_entity_pre_stamped_with_a_different_tenant(
    scoped_repository, make_widget
):
    with tenant_scope("tenant-a"), pytest.raises(CrossTenantAccessError):
        await scoped_repository.create(make_widget(tenant_id="tenant-other"))


async def test_get_by_id_hides_records_from_other_tenants(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="secret"))

    with tenant_scope("tenant-b"):
        assert await scoped_repository.get_by_id(created.id) is None

    with tenant_scope("tenant-a"):
        assert await scoped_repository.get_by_id(created.id) is not None


async def test_update_rejects_an_attempt_to_reassign_tenant_id(scoped_repository, make_widget):
    """A tenant must not be able to hijack its own record into another tenant
    by smuggling ``tenant_id`` through ``changes`` (regression test for a
    tenant-isolation bypass: update() used to forward ``changes`` verbatim)."""
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="mine"))

        with pytest.raises(CrossTenantAccessError):
            await scoped_repository.update(created.id, {"tenant_id": "tenant-b"})

        # the record must still belong to tenant-a and be unmodified
        untouched = await scoped_repository.get_by_id(created.id)

    assert untouched is not None
    assert untouched.tenant_id == "tenant-a"

    with tenant_scope("tenant-b"):
        assert await scoped_repository.get_by_id(created.id) is None


async def test_update_allows_setting_tenant_id_to_its_own_current_value(
    scoped_repository, make_widget
):
    """A no-op tenant_id in changes (e.g. from a naive full-model dump) is fine."""
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="mine"))
        updated = await scoped_repository.update(
            created.id, {"name": "renamed", "tenant_id": "tenant-a"}
        )

    assert updated.name == "renamed"
    assert updated.tenant_id == "tenant-a"


async def test_update_on_an_owned_record_succeeds(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="owned"))
        updated = await scoped_repository.update(created.id, {"name": "renamed"})

    assert updated.name == "renamed"
    assert updated.tenant_id == "tenant-a"


async def test_update_on_another_tenants_record_raises_not_found(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="owned"))

    with tenant_scope("tenant-b"):
        from krossdb.core.exceptions import RecordNotFoundError

        with pytest.raises(RecordNotFoundError):
            await scoped_repository.update(created.id, {"name": "hijacked"})


async def test_delete_on_an_owned_record_succeeds(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="owned"))

        assert await scoped_repository.delete(created.id) is True
        assert await scoped_repository.get_by_id(created.id) is None


async def test_delete_on_another_tenants_record_is_a_no_op(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget(name="owned"))

    with tenant_scope("tenant-b"):
        assert await scoped_repository.delete(created.id) is False

    with tenant_scope("tenant-a"):
        assert await scoped_repository.get_by_id(created.id) is not None


async def test_find_is_automatically_scoped_to_the_active_tenant(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        await scoped_repository.create(make_widget(name="a-widget"))
    with tenant_scope("tenant-b"):
        await scoped_repository.create(make_widget(name="b-widget"))

    with tenant_scope("tenant-a"):
        page = await scoped_repository.find()

    assert page.total == 1
    assert page.items[0].name == "a-widget"


async def test_count_is_automatically_scoped_to_the_active_tenant(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        await scoped_repository.create(make_widget(name="a-widget-1"))
        await scoped_repository.create(make_widget(name="a-widget-2"))
    with tenant_scope("tenant-b"):
        await scoped_repository.create(make_widget(name="b-widget"))

    with tenant_scope("tenant-a"):
        assert await scoped_repository.count() == 2

    with tenant_scope("tenant-b"):
        assert await scoped_repository.count() == 1


async def test_bulk_create_stamps_every_entity(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        result = await scoped_repository.bulk_create([make_widget(name=f"w{i}") for i in range(3)])

    assert result.all_succeeded
    assert all(w.tenant_id == "tenant-a" for w in result.succeeded)


async def test_bulk_create_rejects_an_entity_pre_stamped_with_a_different_tenant(
    scoped_repository, make_widget
):
    with tenant_scope("tenant-a"), pytest.raises(CrossTenantAccessError):
        await scoped_repository.bulk_create(
            [make_widget(name="ok"), make_widget(name="foreign", tenant_id="tenant-other")]
        )


async def test_bulk_delete_skips_ids_owned_by_other_tenants(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        owned = await scoped_repository.create(make_widget(name="mine"))
    with tenant_scope("tenant-b"):
        foreign = await scoped_repository.create(make_widget(name="not-mine"))

    with tenant_scope("tenant-a"):
        deleted = await scoped_repository.bulk_delete([owned.id, foreign.id, uuid.uuid4()])

    assert deleted == 1


async def test_bulk_update_applies_changes_only_to_records_owned_by_the_active_tenant(
    scoped_repository, make_widget
):
    with tenant_scope("tenant-a"):
        owned = await scoped_repository.create(make_widget(name="mine"))
    with tenant_scope("tenant-b"):
        foreign = await scoped_repository.create(make_widget(name="not-mine"))

    with tenant_scope("tenant-a"):
        result = await scoped_repository.bulk_update(
            {owned.id: {"name": "renamed"}, foreign.id: {"name": "hijacked"}}
        )

    assert result.all_succeeded
    assert [w.id for w in result.succeeded] == [owned.id]
    assert result.succeeded[0].name == "renamed"

    with tenant_scope("tenant-b"):
        untouched = await scoped_repository.get_by_id(foreign.id)

    assert untouched is not None
    assert untouched.name == "not-mine"


async def test_bulk_update_with_no_owned_ids_updates_nothing(scoped_repository, make_widget):
    with tenant_scope("tenant-b"):
        foreign = await scoped_repository.create(make_widget(name="not-mine"))

    with tenant_scope("tenant-a"):
        result = await scoped_repository.bulk_update({foreign.id: {"name": "hijacked"}})

    assert result.succeeded == []
    assert result.failed == {}


async def test_bulk_update_rejects_an_attempt_to_reassign_tenant_id(scoped_repository, make_widget):
    """Regression test: bulk_update() used to forward every change set verbatim,
    so a batch could smuggle a foreign tenant_id past the per-id ownership check."""
    with tenant_scope("tenant-a"):
        owned = await scoped_repository.create(make_widget(name="mine"))

        with pytest.raises(CrossTenantAccessError):
            await scoped_repository.bulk_update(
                {owned.id: {"name": "renamed", "tenant_id": "tenant-b"}}
            )

        untouched = await scoped_repository.get_by_id(owned.id)

    assert untouched is not None
    assert untouched.name == "mine"
    assert untouched.tenant_id == "tenant-a"


# --------------------------------------------------------------------------
# No active tenant context: every operation must fail closed
# --------------------------------------------------------------------------


async def test_get_by_id_without_tenant_context_raises(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget())

    with pytest.raises(TenantContextMissingError):
        await scoped_repository.get_by_id(created.id)


async def test_update_without_tenant_context_raises(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget())

    with pytest.raises(TenantContextMissingError):
        await scoped_repository.update(created.id, {"name": "renamed"})


async def test_delete_without_tenant_context_raises(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget())

    with pytest.raises(TenantContextMissingError):
        await scoped_repository.delete(created.id)


async def test_find_without_tenant_context_raises(scoped_repository):
    with pytest.raises(TenantContextMissingError):
        await scoped_repository.find()


async def test_count_without_tenant_context_raises(scoped_repository):
    with pytest.raises(TenantContextMissingError):
        await scoped_repository.count()


async def test_bulk_create_without_tenant_context_raises(scoped_repository, make_widget):
    with pytest.raises(TenantContextMissingError):
        await scoped_repository.bulk_create([make_widget()])


async def test_bulk_update_without_tenant_context_raises(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget())

    with pytest.raises(TenantContextMissingError):
        await scoped_repository.bulk_update({created.id: {"name": "renamed"}})


async def test_bulk_delete_without_tenant_context_raises(scoped_repository, make_widget):
    with tenant_scope("tenant-a"):
        created = await scoped_repository.create(make_widget())

    with pytest.raises(TenantContextMissingError):
        await scoped_repository.bulk_delete([created.id])


# --------------------------------------------------------------------------
# Concurrency: contextvars must not leak between concurrently running tasks
# --------------------------------------------------------------------------


async def test_concurrent_tenant_scopes_do_not_leak_across_asyncio_tasks(
    scoped_repository, make_widget
):
    """Each ``asyncio.create_task`` gets its own copy of the current context
    (per ``contextvars`` semantics), so two tenants operating concurrently
    through the *same* repository instance must never see each other's data,
    even when their operations interleave."""

    async def create_and_read_back(tenant_id: str, name: str) -> tuple[str, bool]:
        with tenant_scope(tenant_id):
            created = await scoped_repository.create(make_widget(name=name))
            await asyncio.sleep(0)  # yield control so the two tasks interleave
            found = await scoped_repository.get_by_id(created.id)
            await asyncio.sleep(0)
            page = await scoped_repository.find()
            return name, found is not None and all(w.tenant_id == tenant_id for w in page.items)

    results = await asyncio.gather(
        create_and_read_back("tenant-a", "a-widget"),
        create_and_read_back("tenant-b", "b-widget"),
    )

    assert dict(results) == {"a-widget": True, "b-widget": True}
