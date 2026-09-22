"""Unit tests for the driver-specific mappers in :mod:`krossdb.core.exception_mapper`.

``bootstrap_default_mappers`` registers a mapper for each optional driver that is
importable in the current environment. The ``mongodb``/``vector`` dev extras
pull in pymongo, qdrant-client, and SQLAlchemy, so those mappers are exercised
here directly against real driver exception types. asyncpg is only present
when the ``postgres`` extra is installed (as in the ``integration-tests`` CI
job), so that mapper's tests are skipped otherwise.
"""

from __future__ import annotations

import sys

import pytest

from krossdb.core import exceptions as exc
from krossdb.core.exception_mapper import (
    _register_asyncpg_mapper,
    _register_pymongo_mapper,
    _register_qdrant_mapper,
    _register_sqlalchemy_mapper,
    _registry,
    bootstrap_default_mappers,
    translate_sync,
)

pytestmark = pytest.mark.unit


def _assert_mapped(
    mapped: exc.KrossDBError,
    expected_type: type[exc.KrossDBError],
    original: BaseException,
    *,
    message_contains: str | None = None,
) -> None:
    """Shared assertion: exception type, message content, and cause chaining."""

    assert isinstance(mapped, expected_type)
    assert mapped.cause is original
    if message_contains is not None:
        assert message_contains.lower() in mapped.message.lower()


# --------------------------------------------------------------------------
# SQLAlchemy
# --------------------------------------------------------------------------


def test_sqlalchemy_integrity_error_with_unique_message_maps_to_duplicate():
    from sqlalchemy.exc import IntegrityError

    orig = Exception("duplicate key value violates unique constraint")
    error = IntegrityError("INSERT ...", {}, orig)

    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.DuplicateRecordError, error, message_contains="unique constraint")


def test_sqlalchemy_integrity_error_without_unique_message_maps_to_generic_violation():
    from sqlalchemy.exc import IntegrityError

    orig = Exception("check constraint failed")
    error = IntegrityError("INSERT ...", {}, orig)

    mapped = translate_sync(error)

    _assert_mapped(
        mapped, exc.IntegrityViolationError, error, message_contains="check constraint failed"
    )
    assert not isinstance(mapped, exc.DuplicateRecordError)


def test_sqlalchemy_timeout_error_maps_to_connection_timeout():
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    error = SATimeoutError("pool exhausted")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.ConnectionTimeoutError, error, message_contains="pool exhausted")


@pytest.mark.parametrize("error_cls_name", ["InterfaceError", "OperationalError"])
def test_sqlalchemy_interface_and_operational_errors_map_to_connection_error(error_cls_name):
    import sqlalchemy.exc as sa_exc

    error_cls = getattr(sa_exc, error_cls_name)
    error = error_cls("bad connection", {}, Exception("gone"))

    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.ConnectionError_, error)


def test_sqlalchemy_integrity_error_chains_through_translate_exceptions():
    """translate_exceptions() must preserve Python's exception chain (__cause__)."""
    from sqlalchemy.exc import IntegrityError

    from krossdb.core.exception_mapper import translate_exceptions

    orig = Exception("duplicate key value violates unique constraint")
    error = IntegrityError("INSERT ...", {}, orig)

    with pytest.raises(exc.DuplicateRecordError) as excinfo, translate_exceptions(op="insert"):
        raise error

    assert excinfo.value.__cause__ is error
    assert excinfo.value.cause is error
    assert excinfo.value.context.get("op") == "insert"


def test_register_sqlalchemy_mapper_is_a_noop_when_sqlalchemy_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlalchemy.exc", None)

    _register_sqlalchemy_mapper()  # must not raise


# --------------------------------------------------------------------------
# pymongo
# --------------------------------------------------------------------------


def test_pymongo_duplicate_key_error_maps_to_duplicate_record():
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.DuplicateKeyError("E11000 duplicate key")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.DuplicateRecordError, error, message_contains="E11000")


@pytest.mark.parametrize(
    "error_cls_name", ["ServerSelectionTimeoutError", "NetworkTimeout"]
)
def test_pymongo_timeout_errors_map_to_connection_timeout(error_cls_name):
    import pymongo.errors as pymongo_errors

    error_cls = getattr(pymongo_errors, error_cls_name)
    error = error_cls("no primary available")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.ConnectionTimeoutError, error, message_contains="no primary")


def test_pymongo_connection_failure_maps_to_connection_error():
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.ConnectionFailure("socket closed")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.ConnectionError_, error, message_contains="socket closed")
    assert not isinstance(mapped, exc.ConnectionTimeoutError)


@pytest.mark.parametrize("auth_code", [13, 18])
def test_pymongo_operation_failure_auth_codes_map_to_authentication_error(auth_code):
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.OperationFailure("not authorized", code=auth_code)

    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.AuthenticationError, error, message_contains="not authorized")


def test_pymongo_operation_failure_other_code_maps_to_query_error():
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.OperationFailure("bad command", code=59)

    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.QueryError, error, message_contains="bad command")
    assert not isinstance(mapped, exc.AuthenticationError)


def test_pymongo_generic_error_maps_to_query_error():
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.PyMongoError("unrecognized pymongo failure")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.QueryError, error, message_contains="unrecognized pymongo failure")


def test_register_pymongo_mapper_is_a_noop_when_pymongo_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "pymongo.errors", None)

    _register_pymongo_mapper()  # must not raise


# --------------------------------------------------------------------------
# qdrant-client
# --------------------------------------------------------------------------


def _unexpected_response(status_code: int):
    import httpx
    from qdrant_client.http.exceptions import UnexpectedResponse

    return UnexpectedResponse(
        status_code=status_code,
        reason_phrase="error",
        content=b"{}",
        headers=httpx.Headers(),
    )


def test_qdrant_404_maps_to_record_not_found():
    error = _unexpected_response(404)
    mapped = translate_sync(error)

    assert isinstance(mapped, exc.RecordNotFoundError)
    assert mapped.cause is error
    assert mapped.entity == "qdrant_point"


@pytest.mark.parametrize("status_code", [401, 403])
def test_qdrant_auth_status_codes_map_to_authorization_error(status_code):
    error = _unexpected_response(status_code)
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.AuthorizationError, error)


def test_qdrant_409_maps_to_duplicate_record():
    error = _unexpected_response(409)
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.DuplicateRecordError, error)


def test_qdrant_other_status_maps_to_query_error():
    error = _unexpected_response(500)
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.QueryError, error)
    assert not isinstance(mapped, exc.RecordNotFoundError)


def test_qdrant_response_handling_exception_maps_to_connection_error():
    from qdrant_client.http.exceptions import ResponseHandlingException

    error = ResponseHandlingException(Exception("network down"))
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.ConnectionError_, error, message_contains="network down")


def test_register_qdrant_mapper_is_a_noop_when_qdrant_client_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "qdrant_client.http.exceptions", None)

    _register_qdrant_mapper()  # must not raise


# --------------------------------------------------------------------------
# asyncpg (only present when the ``postgres`` extra is installed)
# --------------------------------------------------------------------------


def test_asyncpg_unique_violation_maps_to_duplicate_record():
    asyncpg = pytest.importorskip("asyncpg")

    error = asyncpg.UniqueViolationError("dup key")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.DuplicateRecordError, error, message_contains="dup key")


@pytest.mark.parametrize("error_cls_name", ["ForeignKeyViolationError", "CheckViolationError"])
def test_asyncpg_constraint_violations_map_to_integrity_violation(error_cls_name):
    asyncpg = pytest.importorskip("asyncpg")

    error_cls = getattr(asyncpg, error_cls_name)
    error = error_cls("constraint violated")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.IntegrityViolationError, error, message_contains="constraint violated")
    assert not isinstance(mapped, exc.DuplicateRecordError)


def test_asyncpg_deadlock_maps_to_deadlock_detected():
    asyncpg = pytest.importorskip("asyncpg")

    error = asyncpg.DeadlockDetectedError("deadlock")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.DeadlockDetectedError, error, message_contains="deadlock")


@pytest.mark.parametrize(
    "error_cls_name", ["InvalidPasswordError", "InvalidAuthorizationSpecificationError"]
)
def test_asyncpg_auth_errors_map_to_authentication_error(error_cls_name):
    asyncpg = pytest.importorskip("asyncpg")

    error_cls = getattr(asyncpg, error_cls_name)
    error = error_cls("bad credentials")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.AuthenticationError, error, message_contains="bad credentials")


@pytest.mark.parametrize("error_cls_name", ["TooManyConnectionsError", "CannotConnectNowError"])
def test_asyncpg_connection_limit_errors_map_to_connection_error(error_cls_name):
    asyncpg = pytest.importorskip("asyncpg")

    error_cls = getattr(asyncpg, error_cls_name)
    error = error_cls("connection refused")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.ConnectionError_, error, message_contains="connection refused")


def test_asyncpg_generic_postgres_error_maps_to_query_error():
    asyncpg = pytest.importorskip("asyncpg")

    error = asyncpg.PostgresError("unrecognized postgres failure")
    mapped = translate_sync(error)

    _assert_mapped(mapped, exc.QueryError, error, message_contains="unrecognized postgres failure")


def test_register_asyncpg_mapper_is_a_noop_when_asyncpg_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "asyncpg", None)

    _register_asyncpg_mapper()  # must not raise


# --------------------------------------------------------------------------
# Registration order / idempotency
# --------------------------------------------------------------------------


_BOOTSTRAP_MAPPER_NAMES = ("sqlalchemy", "asyncpg", "pymongo", "qdrant")


def test_bootstrap_default_mappers_is_idempotent_and_preserves_registration_order():
    """Calling bootstrap twice must not duplicate any mapper it registers.

    ``register_mapper`` replaces an existing entry in place by name, so a
    second bootstrap call must leave the driver mappers it owns registered
    exactly once each, in the same relative order relative to one another
    (other tests in this suite register their own throwaway mapper names,
    so this only asserts about the names bootstrap itself controls).
    """

    def driver_mapper_names() -> list[str]:
        return [name for name, _ in _registry if name in _BOOTSTRAP_MAPPER_NAMES]

    before = driver_mapper_names()

    bootstrap_default_mappers()
    bootstrap_default_mappers()

    after = driver_mapper_names()

    assert len(after) == len(set(after)), "duplicate mapper registration"
    assert set(after) == set(before)
    assert after == before


def test_generic_mapper_still_wins_for_builtin_errors_after_driver_mappers_registered():
    """The stdlib TimeoutError/ConnectionError mapping must not be shadowed by
    a driver mapper matching on a shared base class."""

    mapped_timeout = translate_sync(TimeoutError("stdlib timeout"))
    mapped_connection = translate_sync(ConnectionError("stdlib connection refused"))

    assert isinstance(mapped_timeout, exc.TimeoutError_)
    assert not isinstance(mapped_timeout, exc.ConnectionTimeoutError)
    assert isinstance(mapped_connection, exc.ConnectionError_)
