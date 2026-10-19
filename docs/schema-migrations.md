# Generic schema migrations

`krossdb` owns the migration mechanism. Your application owns every table,
schema snapshot and revision. No application-specific tables, seed records
or business rules are included in the library.

Install `pip install "krossdb[migrations,sqlite]"` (or the `postgres` extra).
The additive planner uses Alembic's schema comparison; existing Alembic
projects and `krossdb schema init-migrations` continue to work.

## Plan before writing

```bash
krossdb schema plan --connection primary --schema schema.yaml --config krossdb.yaml
```

This inspects the **master** database and prints JSON describing each change
and whether it can be applied automatically. It never writes DDL or history.
Unknown schema fields are rejected, so a misspelled constraint/default cannot
be silently omitted from an applied revision.
Exit status is 0 for an additive/no-op plan, 2 when explicit changes are
required, and 1 on an error. The plan reflects the database at inspection
time; application always replans under the migration lock.

Automatic operations include new tables, missing columns, indexes, foreign
keys and UNIQUE constraints. A required column on a populated table needs a
server default or an explicit backfill. Changes to existing types, defaults,
nullability or constraints, dropping columns and renames require an explicit
migration. Unmanaged tables are ignored. An omitted column on a managed table
is reported as a blocked drop, rather than silently deleting data.

The declarative schema also supports composite uniqueness:

```yaml
tables:
  - name: widgets
    columns:
      - {name: id, type: integer, primary_key: true}
      - {name: group_id, type: integer, required: true}
      - {name: code, type: string, length: 20, required: true}
    unique_constraints:
      - name: uq_widgets_group_code
        columns: [group_id, code]
```

## Numbered revisions

Keep a JSON/YAML manifest in **your project**, in increasing version order.
See [examples/migrations.yaml](../examples/migrations.yaml). Each `schema` is
a complete desired snapshot of the managed tables at that revision, not just
the added columns. Do not edit or remove a revision after applying it; append
a new version instead. Keep previous snapshots even as the current schema evolves.

```bash
# Lists pending revision definitions; does not execute SQL or schema changes.
krossdb schema migrate --connection primary --manifest migrations.yaml --dry-run
# Applies pending revisions, replanning each schema snapshot as it is reached.
krossdb schema migrate --connection primary --manifest migrations.yaml
```

`--dry-run` lists revisions, their authored SQL and target schemas. It does
not simulate their future effects on a database. Use `schema plan` for an
actual comparison against the current database. An explicit SQL revision can
be followed by a declarative snapshot to verify the resulting structure.

The runner creates `krossdb_schema_migrations` with version, name, SHA-256
checksum and application time. Changed applied revisions, missing history or
out-of-order histories fail before further changes. Repeating a completed
manifest is a no-op. There is one history stream per database.

## Explicit changes, functions and triggers

For a rename, data conversion, backfill, function or trigger, add trusted
developer-authored SQL and declare the database dialect:

```yaml
migrations:
  # Include all previously applied revisions here before version 3.
  - version: 3
    name: rename_widget_name
    dialect: postgresql
    sql:
      - ALTER TABLE widgets RENAME COLUMN name TO display_name
```

Each `sql` item must be **one driver statement**. A PostgreSQL
`CREATE OR REPLACE FUNCTION ... $$ ... $$ LANGUAGE plpgsql` body may contain
semicolons within that single statement. Define a trigger in a separate item.
Statements execute before the optional `schema` in the same revision. SQL
is not translated between dialects; a dialect mismatch is rejected.

The runner owns transaction control. Do not put `BEGIN`, `COMMIT`, `ROLLBACK`,
PRAGMA, or nontransactional commands in revisions. Common transaction-control
commands and multiple statements per item are rejected. This validation is
not a sandbox for untrusted SQL: authors must not call procedures or SQL that
changes transaction/session semantics or tampers with migration history.

## Python and async startup

```python
from sqlalchemy import create_engine
from krossdb.migrations import MigrationRunner, load_migrations

runner = MigrationRunner(load_migrations("migrations.yaml"))
engine = create_engine("sqlite:///app.db")
with engine.connect() as connection:  # idle connection; no outer begin()
    applied_versions = runner.apply(connection)
engine.dispose()
```

For async projects use `await runner.apply_async(async_engine)`, with the
same async SQLAlchemy engine/driver configuration used by your app. For a
read-only Python preview use `runner.pending(connection)` or
`plan_schema(connection, metadata)`. Inspection starts a normal SQLAlchemy
read transaction; close/rollback that connection before using `apply`.

## Transaction and dialect boundaries

- **PostgreSQL:** all pending DDL, SQL and history share one transaction.
  A transaction advisory lock serializes runners before checking history,
  including first startup. `lock_timeout_ms` defaults to 30 seconds. A failure
  rolls back the complete pending batch. Use a normal READ COMMITTED connection,
  never AUTOCOMMIT.
- **SQLite:** explicit `BEGIN IMMEDIATE` obtains the database write lock and
  makes DDL transactional even with sqlite3 legacy transaction mode. Its wait
  timeout is the SQLite driver's configured busy timeout. New tables can
  declare all constraints. Adding foreign keys or UNIQUE constraints to an
  existing table requires an explicit Alembic batch migration; the automatic
  planner refuses to rebuild tables and risk existing data or triggers.
- **MySQL and other engines:** schema inspection remains available through
  Alembic, but this atomic runner refuses application because MySQL implicitly
  commits DDL. Use the existing Alembic workflow with that backend's guarantees.
- Document/vector databases do not use this relational migration runner.

This is forward migration support. A failed pending batch rolls back; undoing
an already committed revision needs a new explicit forward revision or an
application-owned Alembic downgrade. The runner does not automatically
drop data, infer renames, reset a database, or insert seed records.
