"""``kvseo config`` — inspect configuration and resolved paths."""

from __future__ import annotations

import typer

from kvseo.config import paths

app = typer.Typer(help="Inspect kvseo configuration and paths.", no_args_is_help=True)


@app.command()
def path() -> None:
    """Print the resolved config and data paths for this machine."""
    typer.echo(f"config file: {paths.config_file()}")
    typer.echo(f"config dir:  {paths.config_dir()}")
    typer.echo(f"data dir:    {paths.data_dir()}")
    typer.echo(f"database:    {paths.db_path()}")
