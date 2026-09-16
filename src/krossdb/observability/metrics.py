"""Performance metrics via OpenTelemetry: operation counters and latency histograms."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import metrics

_meter = metrics.get_meter("krossdb")

_operation_counter = _meter.create_counter(
    "krossdb.operations.total", description="Number of database operations executed"
)
_operation_errors = _meter.create_counter(
    "krossdb.operations.errors", description="Number of database operations that raised"
)
_operation_latency = _meter.create_histogram(
    "krossdb.operations.duration_ms", unit="ms", description="Database operation latency"
)


@asynccontextmanager
async def track_latency(operation: str, **attributes: str) -> AsyncIterator[None]:
    """Wrap a DB operation, recording count/latency/error metrics for it.

    Usage::

        async with track_latency("users.get_by_id", adapter="postgres"):
            await repo.get_by_id(user_id)
    """

    attrs = {"operation": operation, **attributes}
    start = time.monotonic()
    try:
        yield
    except Exception:
        _operation_errors.add(1, attrs)
        raise
    finally:
        elapsed_ms = (time.monotonic() - start) * 1000
        _operation_counter.add(1, attrs)
        _operation_latency.record(elapsed_ms, attrs)


__all__ = ["track_latency"]
