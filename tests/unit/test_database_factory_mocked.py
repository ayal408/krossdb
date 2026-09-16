"""Unit tests for DatabaseFactory orchestration, using fully mocked/fake adapters.

No real database driver is touched here — a hand-rolled fake
``AbstractDatabaseAdapter`` stands in for a real backend, isolating the
factory's own lifecycle/registry logic (this is the "mocked adapters" unit
test layer called for by the project's testing strategy; real wire-protocol
behavior is covered separately by the Testcontainers integration tests).
"""

from __future__ import annotations

import pytest

from krossdb.core.config import ConnectionConfig, NodeConfig
from krossdb.core.enums import DatabaseKind, HealthStatus, RoutingRole
from krossdb.core.exceptions import ConfigurationError
from krossdb.factory.db_factory import DatabaseFactory, register_adapter
from krossdb.interfaces.adapter import AbstractDatabaseAdapter, HealthCheckResult

pytestmark = pytest.mark.unit


class FakeAdapter(AbstractDatabaseAdapter[object]):
    kind = DatabaseKind.POSTGRESQL

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self.connect_calls = 0
        self.disconnect_calls = 0

    async def connect(self) -> None:
        self.connect_calls += 1
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    def acquire(self, *, role: RoutingRole = RoutingRole.MASTER):
        raise NotImplementedError

    async def health_check(self) -> HealthCheckResult:
        return HealthCheckResult(status=HealthStatus.HEALTHY, latency_ms=1.0)


@pytest.fixture(autouse=True)
def _register_fake_adapter():
    register_adapter(DatabaseKind.POSTGRESQL, FakeAdapter)
    yield


def _config(name: str = "primary") -> ConnectionConfig:
    return ConnectionConfig(
        name=name,
        kind=DatabaseKind.POSTGRESQL,
        nodes=[NodeConfig(dsn="postgresql+asyncpg://u:p@localhost/db", role=RoutingRole.MASTER)],
    )


def test_duplicate_connection_names_raise_at_construction():
    with pytest.raises(ConfigurationError):
        DatabaseFactory([_config("primary"), _config("primary")])


async def test_connect_all_connects_every_configured_adapter():
    factory = DatabaseFactory([_config("primary"), _config("secondary")])

    await factory.connect_all()

    assert "primary" in factory
    assert "secondary" in factory
    await factory.disconnect_all()


async def test_get_before_connect_raises_configuration_error():
    factory = DatabaseFactory([_config("primary")])

    with pytest.raises(ConfigurationError):
        factory.get("primary")


async def test_get_unknown_connection_name_raises():
    factory = DatabaseFactory([_config("primary")])
    await factory.connect_all()

    with pytest.raises(ConfigurationError):
        factory.get("does-not-exist")

    await factory.disconnect_all()


async def test_async_context_manager_connects_and_disconnects():
    factory = DatabaseFactory([_config("primary")])

    async with factory as active:
        adapter = active.get("primary")
        assert adapter.is_connected is True

    assert "primary" not in factory


async def test_disconnect_all_is_resilient_to_one_adapter_failing(mocker):
    factory = DatabaseFactory([_config("primary"), _config("secondary")])
    await factory.connect_all()

    primary = factory.get("primary")
    mocker.patch.object(primary, "disconnect", side_effect=RuntimeError("boom"))

    await factory.disconnect_all()  # must not raise despite primary failing

    assert factory._adapters == {}


async def test_health_check_all_returns_one_result_per_connection():
    factory = DatabaseFactory([_config("primary"), _config("secondary")])
    await factory.connect_all()

    results = await factory.health_check_all()

    assert set(results.keys()) == {"primary", "secondary"}
    assert all(r.status == HealthStatus.HEALTHY for r in results.values())

    await factory.disconnect_all()
