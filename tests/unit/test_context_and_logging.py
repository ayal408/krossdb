"""Unit tests for correlation-id/tenant context propagation and log masking."""

from __future__ import annotations

import json

import pytest

from krossdb.core.context import correlation_scope, get_correlation_id, get_tenant_id, tenant_scope
from krossdb.core.exceptions import TenantContextMissingError
from krossdb.core.logging import configure_logging, get_logger

pytestmark = pytest.mark.unit


def test_correlation_id_is_stable_within_a_scope():
    with correlation_scope() as cid:
        assert get_correlation_id() == cid
        assert get_correlation_id() == cid  # calling again doesn't mint a new one


def test_correlation_id_can_be_pinned_explicitly():
    with correlation_scope("fixed-id-123") as cid:
        assert cid == "fixed-id-123"
        assert get_correlation_id() == "fixed-id-123"


def test_tenant_scope_binds_and_unbinds():
    assert get_tenant_id() is None

    with tenant_scope("tenant-a") as tid:
        assert tid == "tenant-a"
        assert get_tenant_id() == "tenant-a"

    assert get_tenant_id() is None


def test_get_tenant_id_required_raises_outside_scope():
    with pytest.raises(TenantContextMissingError):
        get_tenant_id(required=True)


def test_tenant_scope_rejects_empty_string():
    with pytest.raises(TenantContextMissingError), tenant_scope(""):
        pass


def test_log_masking_redacts_sensitive_keys(capsys: pytest.CaptureFixture[str]):
    configure_logging(json_output=True)
    logger = get_logger("test.masking")

    logger.info("user_login", username="alice", password="hunter2", api_key="sk-secret")

    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)

    assert payload["password"] == "***REDACTED***"
    assert payload["api_key"] == "***REDACTED***"
    assert payload["username"] == "alice"


def test_log_masking_redacts_email_in_string_values(capsys: pytest.CaptureFixture[str]):
    configure_logging(json_output=True)
    logger = get_logger("test.masking")

    logger.info("contact_updated", note="reach me at alice@example.com please")

    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)

    assert "alice@example.com" not in payload["note"]
    assert "***REDACTED***" in payload["note"]


def test_logs_include_correlation_id(capsys: pytest.CaptureFixture[str]):
    configure_logging(json_output=True)
    logger = get_logger("test.correlation")

    with correlation_scope("corr-xyz"):
        logger.info("something_happened")

    out = capsys.readouterr().out.strip().splitlines()[-1]
    payload = json.loads(out)

    assert payload["correlation_id"] == "corr-xyz"
