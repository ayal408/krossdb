"""Exercises the AbstractRepository contract via the in-memory reference impl.

Any real backend-specific repository (Postgres, Mongo, Qdrant) should satisfy
the same behavioral contract asserted here.
"""

from __future__ import annotations

import uuid

import pytest

from krossdb.core.exceptions import RecordNotFoundError

pytestmark = pytest.mark.unit


async def test_create_and_get_round_trip(widget_repository, make_widget):
    widget = make_widget(name="widget-a")

    created = await widget_repository.create(widget)

    assert created.id == widget.id
    fetched = await widget_repository.get_by_id(widget.id)
    assert fetched == created


async def test_get_by_id_returns_none_for_missing(widget_repository):
    assert await widget_repository.get_by_id(uuid.uuid4()) is None


async def test_get_by_id_or_raise_raises_record_not_found(widget_repository):
    with pytest.raises(RecordNotFoundError):
        await widget_repository.get_by_id_or_raise(uuid.uuid4())


async def test_update_applies_partial_changes(widget_repository, make_widget):
    widget = await widget_repository.create(make_widget(name="before", quantity=1))

    updated = await widget_repository.update(widget.id, {"name": "after"})

    assert updated.name == "after"
    assert updated.quantity == 1  # untouched field is preserved


async def test_update_missing_raises(widget_repository):
    with pytest.raises(RecordNotFoundError):
        await widget_repository.update(uuid.uuid4(), {"name": "x"})


async def test_delete_returns_true_then_false(widget_repository, make_widget):
    widget = await widget_repository.create(make_widget())

    assert await widget_repository.delete(widget.id) is True
    assert await widget_repository.delete(widget.id) is False


async def test_find_filters_by_criteria(widget_repository, make_widget):
    await widget_repository.create(make_widget(name="alpha", quantity=1))
    await widget_repository.create(make_widget(name="beta", quantity=2))

    page = await widget_repository.find({"quantity": 2})

    assert page.total == 1
    assert page.items[0].name == "beta"


async def test_find_pagination(widget_repository, make_widget):
    for i in range(5):
        await widget_repository.create(make_widget(name=f"widget-{i}"))

    page = await widget_repository.find(limit=2, offset=1)

    assert len(page.items) == 2
    assert page.total == 5
    assert page.has_more is True


async def test_exists_delegates_to_count(widget_repository, make_widget):
    assert await widget_repository.exists({"name": "ghost"}) is False
    await widget_repository.create(make_widget(name="ghost"))
    assert await widget_repository.exists({"name": "ghost"}) is True


async def test_bulk_create_and_bulk_delete(widget_repository, make_widget):
    widgets = [make_widget(name=f"bulk-{i}") for i in range(3)]

    result = await widget_repository.bulk_create(widgets)

    assert result.all_succeeded
    assert result.success_count == 3

    deleted = await widget_repository.bulk_delete([w.id for w in widgets])
    assert deleted == 3


async def test_bulk_update_reports_partial_failure(widget_repository, make_widget):
    widget = await widget_repository.create(make_widget())
    missing_id = uuid.uuid4()

    result = await widget_repository.bulk_update(
        {widget.id: {"name": "updated"}, missing_id: {"name": "nope"}}
    )

    assert result.success_count == 1
    assert result.failure_count == 1
    assert not result.all_succeeded
