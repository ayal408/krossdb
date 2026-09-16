"""`krossdb health` — run adapter health checks from a config file and report status."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True)


@app.callback(invoke_without_command=True)
def check(
    config: Path = typer.Option(Path("krossdb.yaml"), "--config", "-c", help="Path to the connection config file"),
) -> None:
    """Connect to every configured database and report per-connection health status."""

    if not config.exists():
        typer.secho(f"Config file not found: {config}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    asyncio.run(_run_health_checks(config))


async def _run_health_checks(config_path: Path) -> None:
    import yaml

    from krossdb.core.config import ConnectionConfig
    from krossdb.factory.db_factory import DatabaseFactory

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    configs = [ConnectionConfig.model_validate(c) for c in raw.get("connections", [])]

    if not configs:
        typer.secho("No connections defined in config file.", fg=typer.colors.YELLOW)
        return

    exit_code = 0
    async with DatabaseFactory(configs) as factory:
        results = await factory.health_check_all()
        for name, result in results.items():
            color = {
                "healthy": typer.colors.GREEN,
                "degraded": typer.colors.YELLOW,
                "unhealthy": typer.colors.RED,
            }.get(str(result.status), typer.colors.WHITE)
            typer.secho(
                f"{name}: {result.status} ({result.latency_ms:.1f}ms) {result.detail}".strip(),
                fg=color,
            )
            if str(result.status) == "unhealthy":
                exit_code = 1

    raise typer.Exit(code=exit_code)
