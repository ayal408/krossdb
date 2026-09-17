"""Unit tests for krossdb's optional Alembic migration wiring.

Covers the sync-DSN conversion helper, the ``configure()`` env.py wiring
(with Alembic's own ``context``/engine machinery mocked out — no real Alembic
project or database is involved), the scaffolding templates, and the
``krossdb schema init-migrations`` CLI command.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from typer.testing import CliRunner

from krossdb.cli.commands import schema
from krossdb.core.exceptions import ConfigurationError
from krossdb.migrations.support import configure, sync_dsn_from_async
from krossdb.migrations.templates import SCRIPT_PY_MAKO, render_alembic_ini, render_env_py

pytestmark = pytest.mark.unit

runner = CliRunner()


class DummyMetadata:
    """Stand-in for a sqlalchemy.MetaData instance; configure() never introspects it."""


def _write_config(tmp_path: Path, *, dsn: str) -> Path:
    config_path = tmp_path / "krossdb.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "connections": [
                    {
                        "name": "primary",
                        "kind": "postgresql",
                        "nodes": [{"dsn": dsn, "role": "master"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return config_path


class TestSyncDsnFromAsync:
    def test_postgres_asyncpg_to_psycopg(self):
        assert (
            sync_dsn_from_async("postgresql+asyncpg://u:p@host/db")
            == "postgresql+psycopg://u:p@host/db"
        )

    def test_mysql_asyncmy_to_pymysql(self):
        assert sync_dsn_from_async("mysql+asyncmy://u:p@host/db") == "mysql+pymysql://u:p@host/db"

    def test_sqlite_aiosqlite_memory_to_plain_sqlite(self):
        assert sync_dsn_from_async("sqlite+aiosqlite:///:memory:") == "sqlite:///:memory:"

    def test_sqlite_aiosqlite_file_path_preserved(self):
        assert sync_dsn_from_async("sqlite+aiosqlite:///./app.db") == "sqlite:///./app.db"

    def test_unknown_scheme_raises_configuration_error(self):
        with pytest.raises(ConfigurationError):
            sync_dsn_from_async("mongodb://host/db")

    def test_missing_scheme_separator_raises_configuration_error(self):
        with pytest.raises(ConfigurationError):
            sync_dsn_from_async("not-a-dsn")


class TestConfigure:
    def test_offline_mode_configures_context_with_sync_url(self, tmp_path, mocker):
        config_path = _write_config(tmp_path, dsn="postgresql+asyncpg://u:p@host/db")
        metadata = DummyMetadata()

        fake_context = mocker.patch("alembic.context")
        fake_context.is_offline_mode.return_value = True

        configure(metadata, "primary", config_path=config_path)

        fake_context.config.set_main_option.assert_called_once_with(
            "sqlalchemy.url", "postgresql+psycopg://u:p@host/db"
        )
        _, kwargs = fake_context.configure.call_args
        assert kwargs["url"] == "postgresql+psycopg://u:p@host/db"
        assert kwargs["target_metadata"] is metadata
        fake_context.run_migrations.assert_called_once()

    def test_online_mode_builds_engine_and_configures_with_connection(self, tmp_path, mocker):
        config_path = _write_config(tmp_path, dsn="mysql+asyncmy://u:p@host/db")
        metadata = DummyMetadata()

        fake_context = mocker.patch("alembic.context")
        fake_context.is_offline_mode.return_value = False
        fake_context.config.config_ini_section = "alembic"
        fake_context.config.get_section.return_value = {
            "sqlalchemy.url": "mysql+pymysql://u:p@host/db"
        }

        fake_connection = mocker.MagicMock()
        fake_engine = mocker.MagicMock()
        fake_engine.connect.return_value.__enter__.return_value = fake_connection
        mock_engine_from_config = mocker.patch(
            "sqlalchemy.engine_from_config", return_value=fake_engine
        )

        configure(metadata, "primary", config_path=config_path)

        fake_context.config.set_main_option.assert_called_once_with(
            "sqlalchemy.url", "mysql+pymysql://u:p@host/db"
        )
        mock_engine_from_config.assert_called_once()
        fake_context.configure.assert_called_once_with(
            connection=fake_connection, target_metadata=metadata
        )
        fake_context.run_migrations.assert_called_once()

    def test_missing_connection_name_raises_configuration_error(self, tmp_path, mocker):
        config_path = _write_config(tmp_path, dsn="postgresql+asyncpg://u:p@host/db")
        mocker.patch("alembic.context")

        with pytest.raises(ConfigurationError):
            configure(DummyMetadata(), "does-not-exist", config_path=config_path)

    def test_missing_config_file_raises_configuration_error(self, tmp_path, mocker):
        mocker.patch("alembic.context")

        with pytest.raises(ConfigurationError):
            configure(DummyMetadata(), "primary", config_path=tmp_path / "missing.yaml")

    def test_alembic_not_installed_raises_configuration_error(self, tmp_path, mocker):
        config_path = _write_config(tmp_path, dsn="postgresql+asyncpg://u:p@host/db")
        mocker.patch.dict("sys.modules", {"alembic": None})

        with pytest.raises(ConfigurationError, match="migrations"):
            configure(DummyMetadata(), "primary", config_path=config_path)


class TestTemplates:
    def test_render_env_py_embeds_metadata_connection_and_config(self):
        content = render_env_py(
            metadata_path="myapp.db:metadata", connection="primary", config_path="krossdb.yaml"
        )

        assert '"myapp.db:metadata"' in content
        assert 'connection_name="primary"' in content
        assert 'config_path="krossdb.yaml"' in content
        assert "from krossdb.migrations import configure" in content

    def test_render_alembic_ini_has_no_hardcoded_url(self):
        content = render_alembic_ini(alembic_dir="alembic", connection="primary")

        assert "script_location = alembic" in content
        assert "sqlalchemy.url =" not in content
        assert "primary" in content

    def test_script_py_mako_defines_upgrade_and_downgrade(self):
        assert "def upgrade" in SCRIPT_PY_MAKO
        assert "def downgrade" in SCRIPT_PY_MAKO


class TestInitMigrationsCli:
    _ARGS: ClassVar[list[str]] = [
        "init-migrations",
        "--connection",
        "primary",
        "--metadata",
        "myapp.db:metadata",
    ]

    def test_scaffolds_alembic_project(self, tmp_path):
        result = runner.invoke(schema.app, [*self._ARGS, "--directory", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert (tmp_path / "alembic.ini").exists()
        assert (tmp_path / "alembic" / "env.py").exists()
        assert (tmp_path / "alembic" / "script.py.mako").exists()
        assert (tmp_path / "alembic" / "versions").is_dir()

        env_py = (tmp_path / "alembic" / "env.py").read_text(encoding="utf-8")
        assert "myapp.db:metadata" in env_py
        assert 'connection_name="primary"' in env_py

    def test_rejects_metadata_path_without_colon(self, tmp_path):
        result = runner.invoke(
            schema.app,
            [
                "init-migrations",
                "--connection",
                "primary",
                "--metadata",
                "no-colon-here",
                "--directory",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 1

    def test_refuses_to_overwrite_without_force(self, tmp_path):
        args = [*self._ARGS, "--directory", str(tmp_path)]

        first = runner.invoke(schema.app, args)
        assert first.exit_code == 0

        second = runner.invoke(schema.app, args)
        assert second.exit_code == 1

        forced = runner.invoke(schema.app, [*args, "--force"])
        assert forced.exit_code == 0
