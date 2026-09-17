"""`krossdb schema` — apply SQLAlchemy metadata and scaffold Alembic migrations."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(no_args_is_help=True)


@app.command("create")
def create(
    connection: str = typer.Option(..., "--connection", help="Name of the relational connection to target"),
    metadata_path: str | None = typer.Option(
        None,
        "--metadata",
        help="Import path to a sqlalchemy.MetaData instance, e.g. 'myapp.db:metadata'",
    ),
    schema_path: Path | None = typer.Option(
        None,
        "--schema",
        help="Path to a declarative YAML/JSON table schema (see krossdb.schema). "
        "Alternative to --metadata; exactly one of the two must be given.",
    ),
    config: Path = typer.Option(Path("krossdb.yaml"), "--config", "-c", help="Path to the connection config file"),
) -> None:
    """Create every table declared on the given SQLAlchemy ``MetaData`` — or declarative schema file."""

    if not config.exists():
        typer.secho(f"Config file not found: {config}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if bool(metadata_path) == bool(schema_path):
        typer.secho("Pass exactly one of --metadata or --schema", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if schema_path is not None:
        from krossdb.schema import build_metadata, load_schema_config

        metadata = build_metadata(load_schema_config(schema_path))
    else:
        assert metadata_path is not None
        module_path, _, attr = metadata_path.partition(":")
        if not attr:
            typer.secho("--metadata must be in the form 'module.path:attribute_name'", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        module = importlib.import_module(module_path)
        metadata = getattr(module, attr)

    asyncio.run(_create_all(config, connection, metadata))


async def _create_all(config_path: Path, connection_name: str, metadata: Any) -> None:
    import yaml

    from krossdb.core.config import ConnectionConfig
    from krossdb.factory.db_factory import DatabaseFactory

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    configs = [ConnectionConfig.model_validate(c) for c in raw.get("connections", [])]

    async with DatabaseFactory(configs) as factory:
        adapter = factory.get(connection_name)
        async with adapter.acquire() as session:
            await session.run_sync(lambda sync_conn: metadata.create_all(sync_conn))
            await session.commit()

    typer.secho(
        f"Created {len(metadata.tables)} table(s) on {connection_name!r}", fg=typer.colors.GREEN
    )


@app.command("init-migrations")
def init_migrations(
    connection: str = typer.Option(
        ..., "--connection", help="Name of the relational connection to target"
    ),
    metadata_path: str = typer.Option(
        ...,
        "--metadata",
        help="Import path to a sqlalchemy.MetaData instance, e.g. 'myapp.db:metadata'",
    ),
    config: Path = typer.Option(
        Path("krossdb.yaml"), "--config", "-c", help="Path to the connection config file"
    ),
    directory: Path = typer.Option(
        Path("."),
        "--directory",
        "-d",
        help="Project root to scaffold into (alembic.ini + alembic/ are created here)",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
) -> None:
    """Scaffold a starter Alembic project pre-wired to krossdb's own config.

    Alembic's own CLI (``alembic revision --autogenerate``, ``alembic upgrade
    head``, ...) remains the day-to-day tool; this only replaces its default
    ``env.py``/``alembic.ini`` boilerplate with one that reads krossdb.yaml.
    """

    from krossdb.migrations.templates import SCRIPT_PY_MAKO, render_alembic_ini, render_env_py

    _, _, attr = metadata_path.partition(":")
    if not attr:
        typer.secho(
            "--metadata must be in the form 'module.path:attribute_name'", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)

    alembic_dir = directory / "alembic"
    ini_path = directory / "alembic.ini"
    versions_dir = alembic_dir / "versions"

    targets = {
        ini_path: render_alembic_ini(alembic_dir="alembic", connection=connection),
        alembic_dir / "env.py": render_env_py(
            metadata_path=metadata_path, connection=connection, config_path=str(config)
        ),
        alembic_dir / "script.py.mako": SCRIPT_PY_MAKO,
    }

    existing = [path for path in targets if path.exists()]
    if existing and not force:
        names = ", ".join(str(path) for path in existing)
        typer.secho(f"Already exists (pass --force to overwrite): {names}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / ".gitkeep").touch(exist_ok=True)
    for path, content in targets.items():
        path.write_text(content, encoding="utf-8")

    typer.secho(
        f"Scaffolded Alembic project in {directory / 'alembic'} (+ {ini_path})",
        fg=typer.colors.GREEN,
    )
