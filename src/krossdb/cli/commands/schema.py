"""`krossdb schema create` — apply SQLAlchemy metadata (create tables) against a configured connection."""

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
    metadata_path: str = typer.Option(
        ...,
        "--metadata",
        help="Import path to a sqlalchemy.MetaData instance, e.g. 'myapp.db:metadata'",
    ),
    config: Path = typer.Option(Path("krossdb.yaml"), "--config", "-c", help="Path to the connection config file"),
) -> None:
    """Create every table declared on the given SQLAlchemy ``MetaData`` object."""

    if not config.exists():
        typer.secho(f"Config file not found: {config}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

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

    typer.secho(f"Created {len(metadata.tables)} table(s) on {connection_name!r}", fg=typer.colors.GREEN)
