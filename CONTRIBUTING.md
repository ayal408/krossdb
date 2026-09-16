# Contributing to krossdb

## Setup

```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
```

## Running tests

```bash
pytest -m unit          # fast, no external services
pytest -m integration   # spins up real Postgres/MongoDB/Qdrant via Docker
pytest                  # everything
```

## Before opening a PR

```bash
ruff check src tests
mypy src
pytest
```

## Style

- No comments explaining *what* code does — only *why*, when it's non-obvious.
- Every raised error should be a `KrossDBError` subclass (driver exceptions are
  translated centrally in `core/exception_mapper.py`).
- New adapters/repositories should follow the existing pattern: register with
  `krossdb.factory.db_factory.register_adapter` and implement the relevant ABC
  from `krossdb.interfaces`.
