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
    translate_sync,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# SQLAlchemy
# --------------------------------------------------------------------------


def test_sqlalchemy_integrity_error_with_unique_message_maps_to_duplicate():
    from sqlalchemy.exc import IntegrityError

    orig = Exception("duplicate key value violates unique constraint")
    error = IntegrityError("INSERT ...", {}, orig)

    mapped = translate_sync(error)

    assert isinstance(mapped, exc.DuplicateRecordError)


def test_sqlalchemy_integrity_error_without_unique_message_maps_to_generic_violation():
    from sqlalchemy.exc import IntegrityError

    orig = Exception("check constraint failed")
    error = IntegrityError("INSERT ...", {}, orig)

    mapped = translate_sync(error)

    assert isinstance(mapped, exc.IntegrityViolationError)
    assert not isinstance(mapped, exc.DuplicateRecordError)


def test_sqlalchemy_timeout_error_maps_to_connection_timeout():
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    mapped = translate_sync(SATimeoutError("pool exhausted"))

    assert isinstance(mapped, exc.ConnectionTimeoutError)


@pytest.mark.parametrize("error_cls_name", ["InterfaceError", "OperationalError"])
def test_sqlalchemy_interface_and_operational_errors_map_to_connection_error(error_cls_name):
    import sqlalchemy.exc as sa_exc

    error_cls = getattr(sa_exc, error_cls_name)
    error = error_cls("bad connection", {}, Exception("gone"))

    mapped = translate_sync(error)

    assert isinstance(mapped, exc.ConnectionError_)


def test_register_sqlalchemy_mapper_is_a_noop_when_sqlalchemy_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlalchemy.exc", None)

    _register_sqlalchemy_mapper()  # must not raise


# --------------------------------------------------------------------------
# pymongo
# --------------------------------------------------------------------------


def test_pymongo_duplicate_key_error_maps_to_duplicate_record():
    import pymongo.errors as pymongo_errors

    mapped = translate_sync(pymongo_errors.DuplicateKeyError("E11000 duplicate key"))

    assert isinstance(mapped, exc.DuplicateRecordError)


@pytest.mark.parametrize(
    "error_cls_name", ["ServerSelectionTimeoutError", "NetworkTimeout"]
)
def test_pymongo_timeout_errors_map_to_connection_timeout(error_cls_name):
    import pymongo.errors as pymongo_errors

    error_cls = getattr(pymongo_errors, error_cls_name)
    mapped = translate_sync(error_cls("no primary available"))

    assert isinstance(mapped, exc.ConnectionTimeoutError)


def test_pymongo_connection_failure_maps_to_connection_error():
    import pymongo.errors as pymongo_errors

    mapped = translate_sync(pymongo_errors.ConnectionFailure("socket closed"))

    assert isinstance(mapped, exc.ConnectionError_)
    assert not isinstance(mapped, exc.ConnectionTimeoutError)


@pytest.mark.parametrize("auth_code", [13, 18])
def test_pymongo_operation_failure_auth_codes_map_to_authentication_error(auth_code):
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.OperationFailure("not authorized", code=auth_code)

    mapped = translate_sync(error)

    assert isinstance(mapped, exc.AuthenticationError)


def test_pymongo_operation_failure_other_code_maps_to_query_error():
    import pymongo.errors as pymongo_errors

    error = pymongo_errors.OperationFailure("bad command", code=59)

    mapped = translate_sync(error)

    assert isinstance(mapped, exc.QueryError)
    assert not isinstance(mapped, exc.AuthenticationError)


def test_pymongo_generic_error_maps_to_query_error():
    import pymongo.errors as pymongo_errors

    mapped = translate_sync(pymongo_errors.PyMongoError("unrecognized pymongo failure"))

    assert isinstance(mapped, exc.QueryError)


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
    mapped = translate_sync(_unexpected_response(404))

    assert isinstance(mapped, exc.RecordNotFoundError)


@pytest.mark.parametrize("status_code", [401, 403])
def test_qdrant_auth_status_codes_map_to_authorization_error(status_code):
    mapped = translate_sync(_unexpected_response(status_code))

    assert isinstance(mapped, exc.AuthorizationError)


def test_qdrant_409_maps_to_duplicate_record():
    mapped = translate_sync(_unexpected_response(409))

    assert isinstance(mapped, exc.DuplicateRecordError)


def test_qdrant_other_status_maps_to_query_error():
    mapped = translate_sync(_unexpected_response(500))

    assert isinstance(mapped, exc.QueryError)
    assert not isinstance(mapped, exc.RecordNotFoundError)


def test_qdrant_response_handling_exception_maps_to_connection_error():
    from qdrant_client.http.exceptions import ResponseHandlingException

    mapped = translate_sync(ResponseHandlingException(Exception("network down")))

    assert isinstance(mapped, exc.ConnectionError_)


def test_register_qdrant_mapper_is_a_noop_when_qdrant_client_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "qdrant_client.http.exceptions", None)

    _register_qdrant_mapper()  # must not raise


# --------------------------------------------------------------------------
# asyncpg (only present when the ``postgres`` extra is installed)
# --------------------------------------------------------------------------


def test_asyncpg_unique_violation_maps_to_duplicate_record():
    asyncpg = pytest.importorskip("asyncpg")

    mapped = translate_sync(asyncpg.UniqueViolationError("dup key"))

    assert isinstance(mapped, exc.DuplicateRecordError)


@pytest.mark.parametrize("error_cls_name", ["ForeignKeyViolationError", "CheckViolationError"])
def test_asyncpg_constraint_violations_map_to_integrity_violation(error_cls_name):
    asyncpg = pytest.importorskip("asyncpg")

    error_cls = getattr(asyncpg, error_cls_name)
    mapped = translate_sync(error_cls("constraint violated"))

    assert isinstance(mapped, exc.IntegrityViolationError)
    assert not isinstance(mapped, exc.DuplicateRecordError)


def test_asyncpg_deadlock_maps_to_deadlock_detected():
    asyncpg = pytest.importorskip("asyncpg")

    mapped = translate_sync(asyncpg.DeadlockDetectedError("deadlock"))

    assert isinstance(mapped, exc.DeadlockDetectedError)


@pytest.mark.parametrize(
    "error_cls_name", ["InvalidPasswordError", "InvalidAuthorizationSpecificationError"]
)
def test_asyncpg_auth_errors_map_to_authentication_error(error_cls_name):
    asyncpg = pytest.importorskip("asyncpg")

    error_cls = getattr(asyncpg, error_cls_name)
    mapped = translate_sync(error_cls("bad credentials"))

    assert isinstance(mapped, exc.AuthenticationError)


@pytest.mark.parametrize("error_cls_name", ["TooManyConnectionsError", "CannotConnectNowError"])
def test_asyncpg_connection_limit_errors_map_to_connection_error(error_cls_name):
    asyncpg = pytest.importorskip("asyncpg")

    error_cls = getattr(asyncpg, error_cls_name)
    mapped = translate_sync(error_cls("connection refused"))

    assert isinstance(mapped, exc.ConnectionError_)


def test_asyncpg_generic_postgres_error_maps_to_query_error():
    asyncpg = pytest.importorskip("asyncpg")

    mapped = translate_sync(asyncpg.PostgresError("unrecognized postgres failure"))

    assert isinstance(mapped, exc.QueryError)


def test_register_asyncpg_mapper_is_a_noop_when_asyncpg_is_unimportable(monkeypatch):
    monkeypatch.setitem(sys.modules, "asyncpg", None)

    _register_asyncpg_mapper()  # must not raise
