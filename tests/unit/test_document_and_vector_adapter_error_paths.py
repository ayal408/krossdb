"""Unit tests for MongoDB/Qdrant adapter behavior that doesn't require a live server.

Connectivity itself (connect/health_check against a real backend) is
exercised by the Testcontainers integration tests; this file covers the
not-connected/disconnected edge cases plus the trivial Postgres/MySQL
dialect subclasses.
"""

from __future__ import annotations

import pytest

from krossdb.adapters.document.mongodb import MongoDBAdapter
from krossdb.adapters.relational.mysql import MySQLAdapter
from krossdb.adapters.relational.postgres import PostgresAdapter
from krossdb.adapters.vector.qdrant import QdrantAdapter
from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import DatabaseKind, HealthStatus, RoutingRole
from krossdb.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit


def _config(kind: DatabaseKind, dsn: str) -> ConnectionConfig:
    return ConnectionConfig(
        name="test",
        kind=kind,
        nodes=[NodeConfig(dsn=dsn, role=RoutingRole.MASTER)],
    )


async def test_mongodb_health_check_reports_unhealthy_when_never_connected():
    adapter = MongoDBAdapter(_config(DatabaseKind.MONGODB, "mongodb://localhost/db"))

    result = await adapter.health_check()

    assert result.status == HealthStatus.UNHEALTHY
    assert "not connected" in result.detail


async def test_mongodb_acquire_raises_when_not_connected():
    adapter = MongoDBAdapter(_config(DatabaseKind.MONGODB, "mongodb://localhost/db"))

    with pytest.raises(ConfigurationError):
        async with adapter.acquire():
            pass


async def test_mongodb_disconnect_before_connect_is_a_safe_no_op():
    adapter = MongoDBAdapter(_config(DatabaseKind.MONGODB, "mongodb://localhost/db"))

    await adapter.disconnect()  # must not raise

    assert adapter.is_connected is False


async def test_qdrant_health_check_reports_unhealthy_when_never_connected():
    adapter = QdrantAdapter(_config(DatabaseKind.QDRANT, "http://localhost:6333"))

    result = await adapter.health_check()

    assert result.status == HealthStatus.UNHEALTHY
    assert "not connected" in result.detail


async def test_qdrant_acquire_raises_when_not_connected():
    adapter = QdrantAdapter(_config(DatabaseKind.QDRANT, "http://localhost:6333"))

    with pytest.raises(ConfigurationError):
        async with adapter.acquire():
            pass


async def test_qdrant_disconnect_before_connect_is_a_safe_no_op():
    adapter = QdrantAdapter(_config(DatabaseKind.QDRANT, "http://localhost:6333"))

    await adapter.disconnect()  # must not raise

    assert adapter.is_connected is False


def test_postgres_adapter_declares_its_kind():
    assert PostgresAdapter.kind == DatabaseKind.POSTGRESQL


def test_mysql_adapter_declares_its_kind():
    assert MySQLAdapter.kind == DatabaseKind.MYSQL
