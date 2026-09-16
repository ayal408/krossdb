"""krossdb CLI entry point (installed as the ``krossdb`` console script)."""

from __future__ import annotations

import typer

from krossdb.cli.commands import health, init, schema

app = typer.Typer(
    name="krossdb",
    help="Manage krossdb-backed database connections: scaffold config, check health, run schema ops.",
    no_args_is_help=True,
)

app.add_typer(init.app, name="init")
app.add_typer(health.app, name="health")
app.add_typer(schema.app, name="schema")


@app.command()
def version() -> None:
    """Print the installed krossdb version."""

    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        typer.echo(pkg_version("krossdb"))
    except PackageNotFoundError:
        typer.echo("0.0.0+local")


if __name__ == "__main__":
    app()
