"""``kvseo init`` — create the config file and local SQLite database."""

from __future__ import annotations

import typer

from kvseo.config import paths
from kvseo.config.settings import DEFAULT_CONFIG_TOML
from kvseo.storage.db import init_db


def init() -> None:
    """Create the kvseo config file and local database (idempotent)."""
    cfg_dir = paths.config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    cfg_file = paths.config_file()
    if cfg_file.exists():
        typer.echo(f"config already present: {cfg_file}")
    else:
        cfg_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        typer.echo(f"wrote config: {cfg_file}")

    db = paths.db_path()
    if db.exists():
        typer.echo(f"database already present: {db}")
    else:
        init_db(db)
        typer.echo(f"initialised database: {db}")

    typer.echo("kvseo is ready. Next: connect a data source with `kvseo connect gsc`.")
