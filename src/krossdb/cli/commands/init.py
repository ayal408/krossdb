"""`krossdb init` — scaffold a starter connection-config file."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True)

_TEMPLATE = """\
# krossdb connection configuration
# Load with: ConnectionConfig.model_validate(yaml.safe_load(open("krossdb.yaml"))["connections"][0])
connections:
  - name: primary
    kind: postgresql
    nodes:
      - dsn: "postgresql+asyncpg://user:password@localhost:5432/app"
        role: master
      # - dsn: "postgresql+asyncpg://user:password@replica-host:5432/app"
      #   role: replica
    pool:
      min_size: 1
      max_size: 10
    retry:
      max_attempts: 5
    circuit_breaker:
      failure_threshold: 5
"""


@app.callback(invoke_without_command=True)
def scaffold(
    output: Path = typer.Option(Path("krossdb.yaml"), "--output", "-o", help="Path to write the config template to"),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing file"),
) -> None:
    """Write a starter ``krossdb.yaml`` connection-config file to the current directory."""

    if output.exists() and not force:
        typer.secho(f"{output} already exists; pass --force to overwrite.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    output.write_text(_TEMPLATE, encoding="utf-8")
    typer.secho(f"Wrote starter config to {output}", fg=typer.colors.GREEN)
