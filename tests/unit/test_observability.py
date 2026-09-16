"""Unit tests for the metrics/tracing context managers.

These use OpenTelemetry's default no-op providers (no collector/exporter is
configured in tests), so they verify the *control flow* — the wrapped
operation still runs, its return value/exception still propagates, spans and
metrics are recorded without raising — rather than actual exported data.
"""

from __future__ import annotations

import pytest

from krossdb.observability.metrics import track_latency
from krossdb.observability.tracing import traced_operation

pytestmark = pytest.mark.unit


async def test_track_latency_runs_the_wrapped_code():
    ran = False

    async with track_latency("widgets.get_by_id", adapter="postgres"):
        ran = True

    assert ran is True


async def test_track_latency_reraises_exceptions_from_the_wrapped_code():
    with pytest.raises(ValueError, match="boom"):
        async with track_latency("widgets.get_by_id"):
            raise ValueError("boom")


async def test_traced_operation_runs_the_wrapped_code():
    ran = False

    async with traced_operation("relational.query", table="widgets"):
        ran = True

    assert ran is True


async def test_traced_operation_reraises_and_records_exceptions():
    with pytest.raises(RuntimeError, match="query failed"):
        async with traced_operation("relational.query", table="widgets"):
            raise RuntimeError("query failed")
