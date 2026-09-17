# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `krossdb.schema`: declarative table definitions in YAML/JSON (tables, columns,
  foreign keys, indexes) built into a real `sqlalchemy.MetaData` via
  `build_metadata`/`load_schema_config`, so the same file produces correct DDL on
  Postgres, MySQL, and SQLite. `krossdb schema create` now accepts `--schema` as an
  alternative to `--metadata`.
- Richer query filters: `Operator`, `FieldFilter`, and the `Specification` alias let
  repository `find`/`count` express comparisons beyond equality (`gt`/`lt`/`in`/
  `contains`/`is_null`/...), across the relational, document, and vector adapters and
  tenant scoping.
- Optional `migrations` extra (`pip install krossdb[migrations]`, pulled in by
  `dev` and `all`) adding real Alembic-based migration tooling on top of the
  existing one-shot `krossdb schema create`.
- `krossdb.migrations.configure()` — a helper a consuming app's own
  `alembic/env.py` calls to resolve Alembic's database URL and target
  `MetaData` from krossdb's own `krossdb.yaml` config (via the same
  `module.path:attribute_name` import convention as `schema create`) instead
  of a hardcoded `sqlalchemy.url` in `alembic.ini`. Converts the async DSN a
  krossdb `ConnectionConfig` carries (`postgresql+asyncpg://`,
  `mysql+asyncmy://`, `sqlite+aiosqlite://`) to a sync-capable one, since
  Alembic drives migrations through a sync SQLAlchemy engine.
- `krossdb schema init-migrations` CLI command: scaffolds a starter
  `alembic.ini` + `alembic/env.py` + `alembic/script.py.mako` +
  `alembic/versions/` pre-wired to `krossdb.migrations.configure()`, so
  day-to-day migration work (`alembic revision --autogenerate`, `alembic
  upgrade head`, `alembic downgrade`, `alembic history`) goes through
  Alembic's own CLI as usual.

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
