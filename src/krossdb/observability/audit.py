"""Automatic audit-trail recording for repository mutations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from krossdb.core.context import get_correlation_id, get_tenant_id
from krossdb.core.logging import get_logger
from krossdb.models.base import AuditRecord

_logger = get_logger("krossdb.observability.audit")


class AbstractAuditSink(ABC):
    """Where audit records go: a table, a collection, a log stream, ..."""

    @abstractmethod
    async def record(self, entry: AuditRecord) -> None: ...


class LoggingAuditSink(AbstractAuditSink):
    """Default sink: emits audit entries as structured log events."""

    async def record(self, entry: AuditRecord) -> None:
        _logger.info(
            "audit_trail",
            entity_type=entry.entity_type,
            entity_id=entry.entity_id,
            action=entry.action,
            actor=entry.actor,
            tenant_id=entry.tenant_id,
            correlation_id=entry.correlation_id,
        )


class AuditTrailRecorder:
    """Builds :class:`AuditRecord` entries with ambient context filled in automatically."""

    def __init__(self, sink: AbstractAuditSink, *, actor: str | None = None) -> None:
        self._sink = sink
        self._default_actor = actor

    async def record(
        self,
        *,
        entity_type: str,
        entity_id: Any,
        action: str,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        actor: str | None = None,
    ) -> None:
        entry = AuditRecord(
            entity_type=entity_type,
            entity_id=str(entity_id),
            action=action,
            actor=actor or self._default_actor,
            tenant_id=get_tenant_id(),
            correlation_id=get_correlation_id(),
            before=before,
            after=after,
        )
        await self._sink.record(entry)


__all__ = ["AbstractAuditSink", "AuditTrailRecorder", "LoggingAuditSink"]
