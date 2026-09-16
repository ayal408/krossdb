"""OpenTelemetry distributed tracing helper for database operations."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from krossdb.core.context import get_correlation_id

_tracer = trace.get_tracer("krossdb")


@asynccontextmanager
async def traced_operation(name: str, **attributes: str) -> AsyncIterator[None]:
    """Wrap a DB operation in an OTel client span tagged with the correlation id.

    Usage::

        async with traced_operation("relational.query", table="users"):
            await session.execute(stmt)
    """

    with _tracer.start_as_current_span(name, kind=SpanKind.CLIENT) as span:
        span.set_attribute("krossdb.correlation_id", get_correlation_id())
        for key, value in attributes.items():
            span.set_attribute(f"krossdb.{key}", value)
        try:
            yield
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


__all__ = ["traced_operation"]
