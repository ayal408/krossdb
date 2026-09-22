"""Exercises the AbstractRepository contract via the in-memory reference impl.

Any real backend-specific repository (Postgres, Mongo, Qdrant) should satisfy
the same behavioral contract asserted here.
"""

from __future__ import annotations

import uuid

import pytest

from krossdb.core.exceptions import RecordNotFoundError, UnsupportedOperationError
from krossdb.interfaces.repository import FieldFilter, Operator, _matches_criteria

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


# --------------------------------------------------------------------------
# extra_criteria: the general write-scoping mechanism TenantScopedRepository
# builds on (see krossdb.multitenancy.context and issue #35). Exercised here
# against the in-memory reference repo; the real backends have their own
# dedicated tests (test_relational_repository_sqlite.py against real SQL,
# and the mocked document/vector suites).
# --------------------------------------------------------------------------


async def test_update_with_matching_extra_criteria_succeeds(widget_repository, make_widget):
    widget = await widget_repository.create(make_widget(name="before", tenant_id="tenant-a"))

    updated = await widget_repository.update(
        widget.id,
        {"name": "after"},
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-a")],
    )

    assert updated.name == "after"


async def test_update_with_non_matching_extra_criteria_raises_and_leaves_row_untouched(
    widget_repository, make_widget
):
    widget = await widget_repository.create(make_widget(name="before", tenant_id="tenant-a"))

    with pytest.raises(RecordNotFoundError):
        await widget_repository.update(
            widget.id,
            {"name": "after"},
            extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-b")],
        )

    untouched = await widget_repository.get_by_id(widget.id)
    assert untouched.name == "before"


async def test_delete_with_non_matching_extra_criteria_returns_false(widget_repository, make_widget):
    widget = await widget_repository.create(make_widget(tenant_id="tenant-a"))

    deleted = await widget_repository.delete(
        widget.id,
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-b")],
    )

    assert deleted is False
    assert await widget_repository.get_by_id(widget.id) is not None


async def test_bulk_delete_with_extra_criteria_only_removes_matching_rows(
    widget_repository, make_widget
):
    owned = await widget_repository.create(make_widget(tenant_id="tenant-a"))
    foreign = await widget_repository.create(make_widget(tenant_id="tenant-b"))

    deleted = await widget_repository.bulk_delete(
        [owned.id, foreign.id],
        extra_criteria=[FieldFilter(field="tenant_id", operator=Operator.EQ, value="tenant-a")],
    )

    assert deleted == 1
    assert await widget_repository.get_by_id(owned.id) is None
    assert await widget_repository.get_by_id(foreign.id) is not None


# --------------------------------------------------------------------------
# _matches_criteria: the shared FieldFilter evaluator extra_criteria checks
# build on for post-write verification (e.g. the vector adapter, which
# can't get a matched-count back from a filter-scoped write).
# --------------------------------------------------------------------------


async def test_matches_criteria_evaluates_every_operator(make_widget):
    widget = make_widget(name="alpha", quantity=5)

    assert _matches_criteria(widget, [FieldFilter(field="quantity", operator=Operator.EQ, value=5)])
    assert not _matches_criteria(
        widget, [FieldFilter(field="quantity", operator=Operator.EQ, value=6)]
    )
    assert _matches_criteria(widget, [FieldFilter(field="quantity", operator=Operator.NE, value=6)])
    assert _matches_criteria(widget, [FieldFilter(field="quantity", operator=Operator.GT, value=4)])
    assert not _matches_criteria(
        widget, [FieldFilter(field="quantity", operator=Operator.GT, value=5)]
    )
    assert _matches_criteria(widget, [FieldFilter(field="quantity", operator=Operator.GTE, value=5)])
    assert _matches_criteria(widget, [FieldFilter(field="quantity", operator=Operator.LT, value=6)])
    assert _matches_criteria(widget, [FieldFilter(field="quantity", operator=Operator.LTE, value=5)])
    assert _matches_criteria(
        widget, [FieldFilter(field="quantity", operator=Operator.IN, value=[4, 5, 6])]
    )
    assert _matches_criteria(
        widget, [FieldFilter(field="quantity", operator=Operator.NOT_IN, value=[1, 2, 3])]
    )
    assert _matches_criteria(
        widget, [FieldFilter(field="name", operator=Operator.CONTAINS, value="lph")]
    )
    assert _matches_criteria(
        widget, [FieldFilter(field="tenant_id", operator=Operator.IS_NULL, value=True)]
    )
    assert not _matches_criteria(
        widget, [FieldFilter(field="name", operator=Operator.IS_NULL, value=True)]
    )


async def test_matches_criteria_ands_every_filter(make_widget):
    widget = make_widget(name="alpha", quantity=5)

    assert _matches_criteria(
        widget,
        [
            FieldFilter(field="name", operator=Operator.EQ, value="alpha"),
            FieldFilter(field="quantity", operator=Operator.EQ, value=5),
        ],
    )
    assert not _matches_criteria(
        widget,
        [
            FieldFilter(field="name", operator=Operator.EQ, value="alpha"),
            FieldFilter(field="quantity", operator=Operator.EQ, value=99),
        ],
    )


async def test_matches_criteria_with_no_criteria_is_vacuously_true(make_widget):
    assert _matches_criteria(make_widget(), None) is True


async def test_matches_criteria_raises_for_unsupported_operator(make_widget):
    bogus = FieldFilter.model_construct(field="quantity", operator="bogus", value=1)

    with pytest.raises(UnsupportedOperationError):
        _matches_criteria(make_widget(), [bogus])
