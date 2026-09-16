# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-08-25

### Added

- Core: unified `KrossDBError` exception hierarchy with a central
  driver-error translation layer (`core/exception_mapper.py`), structlog
  logging with correlation IDs and automatic PII/secret masking,
  contextvar-based correlation/tenant context, Pydantic v2 settings.
- Interfaces: `AbstractDatabaseAdapter`, `AbstractRepository`,
  `AbstractUnitOfWork`, `AbstractCache`, domain event / outbox contracts.
- Adapters + repositories for PostgreSQL/MySQL/SQLite (async SQLAlchemy,
  with master/replica read-write splitting and weighted load balancing),
  MongoDB (Motor), and Qdrant (with similarity search).
- `DatabaseFactory` for dynamic multi-database orchestration.
- `SQLAlchemyUnitOfWork` with savepoint-based partial bulk-insert success
  and task-local session binding (safe under concurrent requests).
- Redis cache-aside implementation, generic idempotency-key manager.
- In-memory event dispatcher and transactional outbox (in-memory + SQL
  table-backed) implementations.
- Resilience: circuit breaker, exponential backoff retry.
- Transparent multi-tenancy isolation (`TenantScopedRepository`).
- Observability: OpenTelemetry metrics and tracing helpers, audit trail
  recorder.
- Typer-based CLI (`krossdb init` / `health` / `schema create`).
- 159 tests (unit with mocked adapters via pytest-mock + integration against
  real Postgres/MongoDB/Qdrant via Testcontainers), 86%+ coverage.
