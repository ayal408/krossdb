# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Bumped minimum versions of all runtime and dev dependencies to their
  current releases (pydantic, SQLAlchemy, structlog, OpenTelemetry, tenacity,
  rich, anyio, the database drivers, and the pytest/mypy/ruff/pre-commit
  toolchain). Added `ignore::ResourceWarning:pymongo.*` and a targeted
  `pytest.PytestUnraisableExceptionWarning` filter to suppress a benign
  unclosed-socket warning that pymongo's lazy `pymongo.auth` import
  triggers under newer pytest, without disabling unraisable-exception
  detection for the rest of the suite.

### Tests

- Added direct unit coverage for every driver-specific mapper in
  `krossdb.core.exception_mapper` (SQLAlchemy, pymongo, qdrant-client, and
  asyncpg where the `postgres` extra is installed), plus the `ImportError`
  fallback each `_register_*_mapper()` takes when its driver isn't
  installed. Each case asserts the mapped exception type, that the message
  carries the driver's original text, and that `.cause` (and, via
  `translate_exceptions`, Python's own `__cause__` chain) points back at the
  raw driver exception. Also covers that `bootstrap_default_mappers()` is
  idempotent (no duplicate/reordered registrations on a second call) and
  that the built-in `TimeoutError`/`ConnectionError` mapping isn't shadowed
  once driver mappers are registered. Raises `exception_mapper.py` coverage
  from 67% to 100%.
- Added the missing success-path and bulk-update coverage for
  `TenantScopedRepository` (`krossdb.multitenancy.context`): an owned
  record's `update`/`delete` succeeding, `count()` being scoped to the
  active tenant, and `bulk_update()` applying changes only to ids owned by
  the active tenant while silently dropping the rest (mirroring
  `bulk_delete`'s existing cross-tenant behavior). Raises
  `multitenancy/context.py` coverage from 80% to 100%.

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
