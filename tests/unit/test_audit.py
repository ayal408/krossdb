"""Unit tests for AuditTrailRecorder, using a fake sink that just collects entries."""

from __future__ import annotations

import pytest

from krossdb.core.context import correlation_scope, tenant_scope
from krossdb.models.base import AuditRecord
from krossdb.observability.audit import AbstractAuditSink, AuditTrailRecorder

pytestmark = pytest.mark.unit


class CollectingAuditSink(AbstractAuditSink):
    def __init__(self) -> None:
        self.entries: list[AuditRecord] = []

    async def record(self, entry: AuditRecord) -> None:
        self.entries.append(entry)


async def test_record_captures_the_given_fields():
    sink = CollectingAuditSink()
    recorder = AuditTrailRecorder(sink, actor="system")

    await recorder.record(entity_type="Widget", entity_id="abc-123", action="create")

    assert len(sink.entries) == 1
    entry = sink.entries[0]
    assert entry.entity_type == "Widget"
    assert entry.entity_id == "abc-123"
    assert entry.action == "create"
    assert entry.actor == "system"


async def test_per_call_actor_overrides_the_default_actor():
    sink = CollectingAuditSink()
    recorder = AuditTrailRecorder(sink, actor="system")

    await recorder.record(entity_type="Widget", entity_id="1", action="update", actor="alice")

    assert sink.entries[0].actor == "alice"


async def test_record_picks_up_ambient_correlation_and_tenant_context():
    sink = CollectingAuditSink()
    recorder = AuditTrailRecorder(sink)

    with correlation_scope("corr-123"), tenant_scope("tenant-a"):
        await recorder.record(entity_type="Widget", entity_id="1", action="delete")

    entry = sink.entries[0]
    assert entry.correlation_id == "corr-123"
    assert entry.tenant_id == "tenant-a"


async def test_record_captures_before_and_after_snapshots():
    sink = CollectingAuditSink()
    recorder = AuditTrailRecorder(sink)

    await recorder.record(
        entity_type="Widget",
        entity_id="1",
        action="update",
        before={"quantity": 1},
        after={"quantity": 2},
    )

    entry = sink.entries[0]
    assert entry.before == {"quantity": 1}
    assert entry.after == {"quantity": 2}
