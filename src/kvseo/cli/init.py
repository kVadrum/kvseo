"""``kvseo init`` — create the config file and local SQLite database."""

from __future__ import annotations

import typer

from kvseo.cli._util import open_db
from kvseo.config import paths
from kvseo.config.settings import DEFAULT_CONFIG_TOML


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
    existed = db.exists()
    # Migrate (idempotent) with the schema-guard → exit-3 mapping; that
    # contract lives in open_db, and init only needs the side effect.
    open_db().dispose()
    typer.echo(f"{'migrated' if existed else 'initialised'} database: {db}")

    typer.echo("kvseo is ready. Next: connect a data source with `kvseo connect gsc`.")
