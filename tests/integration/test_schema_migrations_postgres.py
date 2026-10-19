"""Migration transaction and lock behavior against PostgreSQL in Testcontainers."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from krossdb.core.exception_mapper import translate_exceptions
from krossdb.core.exceptions import ConfigurationError, KrossDBError
from krossdb.migrations import MigrationRunner, MigrationSpec


@pytest_asyncio.fixture
async def migration_pg_engine(postgres_dsn):
    # Give each test its own history stream without touching other test tables.
    namespace = f"migration_test_{uuid.uuid4().hex}"
    admin = create_async_engine(postgres_dsn)
    async with admin.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{namespace}"'))
    engine = create_async_engine(postgres_dsn, connect_args={"server_settings": {"search_path": namespace}})
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as c:
            await c.execute(text(f'DROP SCHEMA "{namespace}" CASCADE'))
        await admin.dispose()


def initial():
    return MigrationSpec.model_validate({"version": 1, "name": "initial", "schema": {"tables": [
        {"name": "widgets", "columns": [
            {"name": "id", "type": "integer", "primary_key": True},
            {"name": "name", "type": "string", "required": True},
        ]},
        {"name": "groups", "columns": [{"name": "id", "type": "integer", "primary_key": True}]},
    ]}})


async def test_postgres_concurrent_startup_checks_history_under_lock(migration_pg_engine):
    runner = MigrationRunner([initial()])
    results = await asyncio.gather(runner.apply_async(migration_pg_engine), runner.apply_async(migration_pg_engine))
    assert sorted(results) == [(), (1,)]
    async with migration_pg_engine.connect() as c:
        assert (await c.execute(text("SELECT count(*) FROM krossdb_schema_migrations"))).scalar() == 1


async def test_postgres_additive_constraints_defaults_and_trigger(migration_pg_engine):
    first = initial()
    await MigrationRunner([first]).apply_async(migration_pg_engine)
    async with migration_pg_engine.begin() as c:
        await c.execute(text("INSERT INTO widgets(id, name) VALUES (1, 'kept')"))
    target = first.schema_config.model_dump()
    widgets = target["tables"][0]
    widgets["columns"] += [
        {"name": "group_id", "type": "integer"},
        {"name": "quantity", "type": "integer", "required": True, "default": 0},
        {"name": "enabled", "type": "boolean", "required": True, "default": False},
    ]
    widgets["foreign_keys"] = [{"columns": ["group_id"], "ref_table": "groups", "ref_columns": ["id"]}]
    widgets["unique_constraints"] = [{"name": "uq_widget_name", "columns": ["name"]}]
    widgets["indexes"] = [{"name": "ix_widget_quantity", "columns": ["quantity"]}]
    second = MigrationSpec.model_validate({"version": 2, "name": "extend", "schema": target})
    third = MigrationSpec(version=3, name="trigger", dialect="postgresql", sql=(
        "CREATE FUNCTION bump_quantity() RETURNS TRIGGER AS $$ BEGIN NEW.quantity := NEW.quantity + 1; RETURN NEW; END; $$ LANGUAGE plpgsql",
        "CREATE TRIGGER widget_bump BEFORE INSERT ON widgets FOR EACH ROW EXECUTE FUNCTION bump_quantity()",
    ))
    runner = MigrationRunner([first, second, third])
    assert await runner.apply_async(migration_pg_engine) == (2, 3)
    assert await runner.apply_async(migration_pg_engine) == ()
    async with migration_pg_engine.begin() as c:
        assert (await c.execute(text("SELECT name, quantity, enabled FROM widgets"))).one() == ("kept", 0, False)
        await c.execute(text("INSERT INTO widgets(id,name) VALUES (2,'triggered')"))
        assert (await c.execute(text("SELECT quantity FROM widgets WHERE id=2"))).scalar() == 1
        constraints = await c.run_sync(lambda conn: inspect(conn).get_unique_constraints("widgets"))
        assert any(item["name"] == "uq_widget_name" for item in constraints)
    async with migration_pg_engine.connect() as c:
        with pytest.raises(KrossDBError):
            async with c.begin():
                # Foreign key addition must enforce integrity, not only exist in metadata.
                with translate_exceptions():
                    await c.execute(text("INSERT INTO widgets(id,name,group_id) VALUES (3,'invalid',999)"))


async def test_postgres_rolls_back_ddl_and_history(migration_pg_engine):
    first = initial()
    broken = MigrationSpec(version=2, name="broken", dialect="postgresql", sql=(
        "ALTER TABLE widgets ADD COLUMN temporary INTEGER",
        "INSERT INTO missing_table VALUES (1)",
    ))
    with pytest.raises(KrossDBError):
        await MigrationRunner([first, broken]).apply_async(migration_pg_engine)
    async with migration_pg_engine.connect() as c:
        tables = await c.run_sync(lambda conn: inspect(conn).get_table_names())
        assert "widgets" not in tables
        assert "krossdb_schema_migrations" not in tables


async def test_postgres_rejects_autocommit(migration_pg_engine):
    autocommit_engine = migration_pg_engine.execution_options(isolation_level="AUTOCOMMIT")
    with pytest.raises(ConfigurationError, match="AUTOCOMMIT"):
        await MigrationRunner([initial()]).apply_async(autocommit_engine)
