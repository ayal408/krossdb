"""Unit tests for the exception hierarchy and the driver-error translation layer."""

from __future__ import annotations

import pytest

from krossdb.core import exceptions as exc
from krossdb.core.exception_mapper import register_mapper, translate_exceptions, translate_sync

pytestmark = pytest.mark.unit


def test_krossdb_error_carries_context_and_cause():
    cause = ValueError("boom")

    error = exc.QueryError("query failed", cause=cause, context={"table": "users"})

    assert error.message == "query failed"
    assert error.cause is cause
    assert error.context == {"table": "users"}
    assert "QueryError" in repr(error)


def test_record_not_found_formats_entity_and_id():
    error = exc.RecordNotFoundError("User", "abc-123")

    assert "User" in str(error)
    assert "abc-123" in str(error)
    assert error.entity == "User"
    assert error.identifier == "abc-123"


def test_bulk_operation_error_tracks_failures_and_successes():
    failures = {2: ValueError("bad row")}

    error = exc.BulkOperationError("bulk insert had failures", failures=failures, succeeded=4)

    assert error.succeeded == 4
    assert error.failures == failures


def test_translate_exceptions_passes_through_existing_krossdb_errors():
    with pytest.raises(exc.RecordNotFoundError), translate_exceptions(op="get"):
        raise exc.RecordNotFoundError("Widget", 1)


def test_translate_exceptions_maps_builtin_timeout_error():
    with pytest.raises(exc.TimeoutError_) as excinfo, translate_exceptions(op="connect"):
        raise TimeoutError("too slow")

    assert excinfo.value.context.get("op") == "connect"
    assert isinstance(excinfo.value.cause, TimeoutError)


def test_translate_exceptions_maps_builtin_connection_error():
    with pytest.raises(exc.ConnectionError_), translate_exceptions():
        raise ConnectionError("refused")


def test_translate_exceptions_falls_back_to_query_error_for_unknown():
    with pytest.raises(exc.QueryError), translate_exceptions():
        raise RuntimeError("something driver-specific and unmapped")


def test_custom_mapper_takes_priority_and_can_be_replaced():
    class FakeDriverError(Exception):
        pass

    def mapper_v1(error: BaseException) -> exc.KrossDBError | None:
        if isinstance(error, FakeDriverError):
            return exc.AuthenticationError("v1 mapped")
        return None

    register_mapper("fake_driver_test", mapper_v1)
    try:
        mapped = translate_sync(FakeDriverError("nope"))
        assert isinstance(mapped, exc.AuthenticationError)
        assert mapped.message == "v1 mapped"

        def mapper_v2(error: BaseException) -> exc.KrossDBError | None:
            if isinstance(error, FakeDriverError):
                return exc.DuplicateRecordError("v2 mapped")
            return None

        register_mapper("fake_driver_test", mapper_v2)  # same name replaces v1
        mapped_again = translate_sync(FakeDriverError("nope"))
        assert isinstance(mapped_again, exc.DuplicateRecordError)
    finally:
        register_mapper("fake_driver_test", lambda e: None)


def test_buggy_mapper_does_not_break_translation():
    def buggy_mapper(error: BaseException) -> exc.KrossDBError | None:
        raise RuntimeError("mapper itself is broken")

    register_mapper("buggy_test", buggy_mapper)
    try:
        mapped = translate_sync(ValueError("original"))
        assert isinstance(mapped, exc.QueryError)
    finally:
        register_mapper("buggy_test", lambda e: None)
