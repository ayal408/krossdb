"""Real SQLite migration boundaries: DDL rollback, history, concurrency and preview."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from krossdb.cli.commands.schema import app
from krossdb.core.exceptions import (
    ConfigurationError,
    KrossDBError,
    TransactionAlreadyActiveError,
    UnsupportedOperationError,
)
from krossdb.migrations import MigrationRunner, MigrationSpec, load_migrations, plan_schema
from krossdb.schema import SchemaConfig, build_metadata

pytestmark = pytest.mark.unit


def schema(*extra_columns):
    return {"tables": [{"name": "widgets", "columns": [
        {"name": "id", "type": "integer", "primary_key": True},
        {"name": "name", "type": "string", "required": True},
        *extra_columns,
    ]}]}


def revision(version=1, definition=None, **kwargs):
    return MigrationSpec.model_validate({
        "version": version, "name": f"revision_{version}",
        **({"schema": definition or schema()} if "sql" not in kwargs else {}), **kwargs,
    })


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    yield engine
    engine.dispose()


def test_schema_evolution_preserves_rows_and_replay_is_noop(engine):
    first = revision()
    second = revision(2, schema({"name": "quantity", "type": "integer", "required": True, "default": 0}))
    with engine.connect() as c:
        assert MigrationRunner([first]).pending(c) == (first,)
        assert not inspect(c).has_table("krossdb_schema_migrations")
        c.rollback()
        assert MigrationRunner([first]).apply(c) == (1,)
        c.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'original')"))
        c.commit()
        assert MigrationRunner([first, second]).apply(c) == (2,)
        assert MigrationRunner([first, second]).apply(c) == ()
        assert c.execute(text("SELECT name, quantity FROM widgets")).one() == ("original", 0)
        assert c.execute(text("SELECT count(*) FROM krossdb_schema_migrations")).scalar() == 2


def test_whole_batch_ddl_and_history_roll_back_on_failure(engine):
    broken = revision(2, sql=("INSERT INTO missing_table VALUES (1)",), dialect="sqlite")
    with engine.connect() as c:
        with pytest.raises(KrossDBError):
            MigrationRunner([revision(), broken]).apply(c)
        assert inspect(c).get_table_names() == []


def test_existing_history_and_data_survive_failed_revision(engine):
    first = revision()
    with engine.connect() as c:
        MigrationRunner([first]).apply(c)
        c.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'kept')"))
        c.commit()
        second = revision(2, sql=("ALTER TABLE widgets ADD COLUMN temporary INTEGER", "DELETE FROM nonexistent"), dialect="sqlite")
        with pytest.raises(KrossDBError):
            MigrationRunner([first, second]).apply(c)
        assert [col["name"] for col in inspect(c).get_columns("widgets")] == ["id", "name"]
        assert c.execute(text("SELECT name FROM widgets")).scalar() == "kept"
        assert c.execute(text("SELECT version FROM krossdb_schema_migrations")).scalar() == 1


def test_history_mismatch_removal_and_out_of_order_are_rejected(engine):
    with engine.connect() as c:
        MigrationRunner([revision()]).apply(c)
        for revisions in [[revision(name="edited")], [], [revision(2)]]:
            with pytest.raises(ConfigurationError):
                MigrationRunner(revisions).apply(c)
    with pytest.raises(ConfigurationError):
        MigrationRunner([revision(2), revision(1)])
    with pytest.raises(ConfigurationError):
        MigrationRunner([revision(), revision()])


def test_required_column_without_backfill_blocks_entire_revision(engine):
    with engine.connect() as c:
        MigrationRunner([revision()]).apply(c)
        c.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'old')"))
        c.commit()
        second = revision(2, schema({"name": "required", "type": "string", "required": True}))
        with pytest.raises(UnsupportedOperationError, match="Backfill"):
            MigrationRunner([revision(), second]).apply(c)
        assert len(inspect(c).get_columns("widgets")) == 2


def test_plan_detects_destructive_changes_without_executing_them(engine):
    with engine.connect() as c:
        MigrationRunner([revision()]).apply(c)
        renamed = schema()
        renamed["tables"][0]["columns"][1]["name"] = "renamed"
        metadata = build_metadata(SchemaConfig.model_validate(renamed))
        plan = plan_schema(c, metadata)
        assert any(change.operation == "DropColumnOp" for change in plan.blocked)
        assert [col["name"] for col in inspect(c).get_columns("widgets")] == ["id", "name"]
        c.rollback()
        with pytest.raises(UnsupportedOperationError):
            MigrationRunner([revision(), revision(2, renamed)]).apply(c)


def test_unmanaged_tables_are_not_removed(engine):
    with engine.connect() as c:
        c.execute(text("CREATE TABLE external (id INTEGER PRIMARY KEY)"))
        c.commit()
        MigrationRunner([revision()]).apply(c)
        assert "external" in inspect(c).get_table_names()


def test_concurrent_runners_apply_each_revision_once(engine):
    runner = MigrationRunner([revision()])

    def apply():
        with engine.connect() as c:
            return runner.apply(c)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: apply(), range(2))) == [(), (1,)]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM krossdb_schema_migrations")).scalar() == 1


@pytest.mark.parametrize("statement", ["COMMIT", "-- comment\nROLLBACK", "SELECT 1; COMMIT;", "PRAGMA foreign_keys=OFF", "/* comment */ BEGIN"])
def test_sql_cannot_escape_the_managed_transaction(engine, statement):
    with engine.connect() as c:
        with pytest.raises(ConfigurationError):
            MigrationRunner([revision(sql=(statement,), dialect="sqlite")]).apply(c)
        assert inspect(c).get_table_names() == []


def test_active_transactions_and_wrong_dialects_are_rejected(engine):
    with engine.connect() as c:
        c.execute(text("SELECT 1"))
        with pytest.raises(TransactionAlreadyActiveError):
            MigrationRunner([revision()]).apply(c)
        c.rollback()
        with pytest.raises(UnsupportedOperationError):
            MigrationRunner([revision(sql=("SELECT 1",), dialect="postgresql")]).apply(c)


async def test_async_engine_uses_the_same_transactional_runner(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'async.db'}")
    try:
        runner = MigrationRunner([revision()])
        assert await runner.apply_async(engine) == (1,)
        assert await runner.apply_async(engine) == ()
    finally:
        await engine.dispose()


def test_cli_preview_apply_and_schema_plan(tmp_path):
    config = tmp_path / "krossdb.yaml"
    config.write_text(yaml.safe_dump({"connections": [{
        "name": "primary", "kind": "sqlite",
        "nodes": [{"dsn": f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}", "role": "master"}],
    }]}))
    manifest = tmp_path / "migrations.yaml"
    manifest.write_text(yaml.safe_dump({"migrations": [{"version": 1, "name": "initial", "schema": schema()}]}))
    args = ["migrate", "--connection", "primary", "--config", str(config), "--manifest", str(manifest)]
    cli = CliRunner()
    preview = cli.invoke(app, [*args, "--dry-run"])
    assert preview.exit_code == 0, preview.output
    assert '"version": 1' in preview.output
    preview_engine = create_engine(f"sqlite:///{tmp_path / 'cli.db'}")
    with preview_engine.connect() as c:
        assert inspect(c).get_table_names() == []
    preview_engine.dispose()
    assert cli.invoke(app, args).exit_code == 0
    assert "Applied revisions: []" in cli.invoke(app, args).output
    schema_path = tmp_path / "schema.yaml"
    schema_path.write_text(yaml.safe_dump(schema({"name": "extra", "type": "integer"})))
    plan = cli.invoke(app, ["plan", "--connection", "primary", "--config", str(config), "--schema", str(schema_path)])
    assert plan.exit_code == 0, plan.output
    assert "AddColumnOp" in plan.output
    assert len(load_migrations(manifest)) == 1
    with pytest.raises(ConfigurationError):
        load_migrations(Path("missing-migration-manifest.json"))


def test_sqlite_constraint_rebuild_and_function_defaults_require_explicit_migration(engine):
    first = revision()
    with engine.connect() as c:
        MigrationRunner([first]).apply(c)
        unique = schema()
        unique["tables"][0]["unique_constraints"] = [{"name": "uq_name", "columns": ["name"]}]
        for target in [unique, schema({"name": "created_at", "type": "datetime", "default": "now"})]:
            plan = plan_schema(c, build_metadata(SchemaConfig.model_validate(target)))
            assert plan.blocked
            c.rollback()
            with pytest.raises(UnsupportedOperationError):
                MigrationRunner([first, revision(2, target)]).apply(c)


def test_composite_uniqueness_is_enforced_and_boolean_defaults_are_native(engine):
    target = schema({"name": "enabled", "type": "boolean", "default": False})
    target["tables"][0]["unique_constraints"] = [{"name": "uq_name_enabled", "columns": ["name", "enabled"]}]
    with engine.connect() as c:
        MigrationRunner([revision(definition=target)]).apply(c)
        c.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'same')"))
        assert c.execute(text("SELECT enabled FROM widgets")).scalar() == 0
        c.commit()
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError):
            c.execute(text("INSERT INTO widgets (id, name) VALUES (2, 'same')"))


def test_cli_blocked_plan_exit_code_and_manifest_error(tmp_path):
    config = tmp_path / "krossdb.yaml"
    db = tmp_path / "cli.db"
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as c:
        MigrationRunner([revision()]).apply(c)
    engine.dispose()
    config.write_text(yaml.safe_dump({"connections": [{"name": "primary", "kind": "sqlite", "nodes": [
        {"dsn": f"sqlite+aiosqlite:///{db}", "role": "master"},
    ]}]}))
    target = schema()
    target["tables"][0]["columns"][1]["type"] = "integer"
    path = tmp_path / "schema.yaml"
    path.write_text(yaml.safe_dump(target))
    cli = CliRunner()
    plan = cli.invoke(app, ["plan", "--connection", "primary", "--config", str(config), "--schema", str(path)])
    assert plan.exit_code == 2, plan.output
    assert "AlterColumnOp" in plan.output
    error = cli.invoke(app, ["migrate", "--connection", "primary", "--config", str(config), "--manifest", str(tmp_path / "absent.yaml")])
    assert error.exit_code == 1
    assert "Invalid migration manifest" in error.output
