# krossdb

[![CI](https://github.com/ayal408/krossdb/actions/workflows/ci.yml/badge.svg)](https://github.com/ayal408/krossdb/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/krossdb.svg)](https://pypi.org/project/krossdb/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

An enterprise-grade, multi-database Python library that provides a unified
interface and repository pattern over relational (PostgreSQL/MySQL/SQLite),
document (MongoDB), and vector (Qdrant) databases.

> Published on PyPI as [`krossdb`](https://pypi.org/project/krossdb/) — the
> name `krossdb` was already taken by an unrelated package. The Python
> package you `import` is unaffected and remains `krossdb`.

## Why

Most projects that touch more than one kind of database end up with
per-backend, ad-hoc data access code, inconsistent error handling, and no
shared story for retries, caching, multi-tenancy, or observability. krossdb
gives every backend the same repository/Unit-of-Work surface, the same
exception hierarchy, and the same resilience and observability behavior, so
application code depends on `krossdb`'s abstractions instead of a specific
driver.

## Architecture at a glance

```
src/krossdb/
├── core/            settings, enums, the exception hierarchy, the driver
│                     exception mapper, structlog config, correlation/tenant
│                     context (contextvars)
├── interfaces/       ABCs: AbstractDatabaseAdapter, AbstractRepository,
│                     AbstractUnitOfWork, AbstractCache, domain events/outbox
├── adapters/         concrete backends, one package per DB family
│   ├── relational/    PostgreSQL / MySQL / SQLite via async SQLAlchemy,
│   │                  with master/replica read-write splitting
│   ├── document/      MongoDB via Motor
│   └── vector/        Qdrant
├── repositories/     backend-specific AbstractRepository implementations
├── uow/               Unit of Work implementations
├── cache/             Redis-backed cache-aside implementation
├── resilience/        circuit breaker + exponential-backoff retry
├── events/             in-process dispatcher + transactional outbox
├── observability/     metrics, tracing, audit trail
├── multitenancy/      tenant isolation helpers
├── factory/            DatabaseFactory: builds/owns every configured adapter
├── models/             shared Pydantic base models (Entity, AuditRecord)
└── cli/                `krossdb` console script (init, health, ...)
```

Every domain entity is a Pydantic v2 model; every mutating repository method
supports an idempotency key; every raised error is a `KrossDBError` subclass
(driver exceptions are translated centrally in
`core/exception_mapper.py`, never leaked to callers).

## Installation

```bash
pip install krossdb             # from PyPI
pip install krossdb[postgres]   # + asyncpg
pip install krossdb[all]        # everything
```

For local development (editable install from a clone):

```bash
pip install -e ".[dev]"          # core + dev/test tooling
pip install -e ".[postgres]"     # + asyncpg
pip install -e ".[all]"          # everything
```

## Quickstart

```python
from krossdb import ConnectionConfig, DatabaseFactory, DatabaseKind, Entity, NodeConfig, RoutingRole
from krossdb.repositories.relational_repository import SQLAlchemyRepository

class Widget(Entity):
    name: str
    quantity: int = 0

config = ConnectionConfig(
    name="primary",
    kind=DatabaseKind.SQLITE,
    nodes=[NodeConfig(dsn="sqlite+aiosqlite:///:memory:", role=RoutingRole.MASTER)],
)

async with DatabaseFactory([config]) as factory:
    repo = SQLAlchemyRepository(factory.get("primary"), widgets_table, Widget)
    widget = await repo.create(Widget(name="gizmo", quantity=10))
```

See [`examples/quickstart.py`](examples/quickstart.py) for a complete, runnable
version (table creation, CRUD, exception handling, and a transactional Unit
of Work) — `python examples/quickstart.py` after `pip install -e ".[sqlite]"`.

## Testing

```bash
pytest -m unit                   # fast, no external services required
pytest -m integration            # spins up real Postgres/MongoDB/Qdrant
                                  # containers via Testcontainers (needs Docker)
pytest                           # everything
```

Integration tests are automatically skipped when Docker isn't reachable, so
`pytest -m unit` (or plain `pytest` in a Docker-less environment) never fails
because of them.

## License

MIT — see [LICENSE](LICENSE).
