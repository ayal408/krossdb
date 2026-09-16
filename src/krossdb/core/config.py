"""Library-wide and per-connection configuration models (Pydantic v2)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from krossdb.core.enums import DatabaseKind, RoutingRole


class RetryConfig(BaseModel):
    """Exponential backoff retry parameters, consumed by :mod:`krossdb.resilience.retry`."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=5, ge=1, le=20)
    initial_backoff_seconds: float = Field(default=0.2, gt=0)
    max_backoff_seconds: float = Field(default=10.0, gt=0)
    multiplier: float = Field(default=2.0, gt=1.0)
    jitter: bool = True


class CircuitBreakerConfig(BaseModel):
    """Thresholds for :mod:`krossdb.resilience.circuit_breaker`."""

    model_config = ConfigDict(frozen=True)

    failure_threshold: int = Field(default=5, ge=1)
    recovery_timeout_seconds: float = Field(default=30.0, gt=0)
    half_open_max_calls: int = Field(default=1, ge=1)


class PoolConfig(BaseModel):
    """Connection pool sizing, shared shape across relational/document/vector adapters."""

    model_config = ConfigDict(frozen=True)

    min_size: int = Field(default=1, ge=0)
    max_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=5, ge=0)
    connect_timeout_seconds: float = Field(default=5.0, gt=0)
    pool_recycle_seconds: float = Field(default=1800.0, gt=0)

    @model_validator(mode="after")
    def _validate_sizes(self) -> PoolConfig:
        if self.min_size > self.max_size:
            raise ValueError("min_size cannot exceed max_size")
        return self


class NodeConfig(BaseModel):
    """A single reachable database endpoint (one master or one replica)."""

    model_config = ConfigDict(frozen=True)

    dsn: SecretStr
    role: RoutingRole = RoutingRole.MASTER
    weight: int = Field(default=1, ge=1, description="Relative weight for weighted load balancing")


class ConnectionConfig(BaseModel):
    """Full configuration for a single logical database (possibly master + replicas)."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(description="Logical name used to look this connection up from the factory")
    kind: DatabaseKind
    nodes: list[NodeConfig] = Field(min_length=1)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    circuit_breaker: CircuitBreakerConfig = Field(default_factory=CircuitBreakerConfig)
    echo: bool = False
    isolation_level: str | None = None

    @model_validator(mode="after")
    def _require_master(self) -> ConnectionConfig:
        if not any(n.role == RoutingRole.MASTER for n in self.nodes):
            raise ValueError(f"connection {self.name!r} must define at least one MASTER node")
        return self

    @property
    def master_nodes(self) -> list[NodeConfig]:
        return [n for n in self.nodes if n.role == RoutingRole.MASTER]

    @property
    def replica_nodes(self) -> list[NodeConfig]:
        return [n for n in self.nodes if n.role == RoutingRole.REPLICA]


class KrossDBSettings(BaseSettings):
    """Process-wide settings, overridable via environment variables prefixed ``KROSSDB_``."""

    model_config = SettingsConfigDict(
        env_prefix="KROSSDB_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    environment: str = "development"
    json_logging: bool = True
    log_level: str = "INFO"
    enable_tracing: bool = False
    otel_exporter_endpoint: str | None = None
    default_cache_ttl_seconds: int = 300
    audit_trail_enabled: bool = True


__all__ = [
    "CircuitBreakerConfig",
    "ConnectionConfig",
    "KrossDBSettings",
    "NodeConfig",
    "PoolConfig",
    "RetryConfig",
]
