"""``kvseo db`` — database maintenance: migrate, backup, vacuum (06 §4.10).

Three operator commands over the single SQLite file. ``migrate`` is the manual
form of what every other command already does on open; ``backup`` (07 §5) and
``vacuum`` are the maintenance pair. SQLite mechanics live in ``storage.db`` per
the layering rule (02 §2) — this module owns wording and exit codes only.

``db prune`` (07 §7) is not here: the CLI reference's own command tree (06 §4.10)
lists three subcommands, and prune is a cascading hard-delete across five tables
that wants its own retention semantics and tests rather than a ride-along.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from kvseo.cli._util import fail, fail_on_storage_refusal, open_db
from kvseo.config import paths
from kvseo.storage import db as storage

app = typer.Typer(help="Maintain the local kvseo database.", no_args_is_help=True)

# Sortable, filename-safe, and distinct from the SQLITE_TS wire format in
# storage.timestamps — that one is a column contract, this one is a filename.
_BACKUP_TS = "%Y%m%d-%H%M%S"


@app.command()
def migrate() -> None:
    """Upgrade the database to the latest schema (idempotent)."""
    db_path = paths.db_path()
    try:
        before = storage.stored_revision(db_path)
    except storage.DatabaseFileError as exc:
        fail_on_storage_refusal(exc)

    # open_db() is the migration: it carries both the upgrade and the exit-3
    # mapping, so this command stays a reporting wrapper around the shared path.
    open_db().dispose()
    after = storage.stored_revision(db_path)

    if before == after:
        typer.echo(f"database already at schema {after}: {db_path}")
    else:
        typer.echo(f"migrated database {before or '(unversioned)'} → {after}: {db_path}")


@app.command()
def backup(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the backup here instead of the default backups/ directory."),
    ] = None,
) -> None:
    """Back up the database to a timestamped file (safe while it is in use)."""
    db_path = paths.db_path()
    if not db_path.exists():
        fail(f"no database at {db_path} — run `kvseo init` first.", code=3)

    # Probing for the revision doubles as the file check, and deliberately
    # without migrate()'s ahead-of-package guard: a corrupt file must still
    # refuse, a database too new for this build must still be backed up.
    try:
        revision = storage.stored_revision(db_path)
    except storage.DatabaseFileError as exc:
        fail_on_storage_refusal(exc)

    dest = output or paths.data_dir() / "backups" / f"kvseo-{datetime.now(UTC).strftime(_BACKUP_TS)}.db"
    if dest.exists():
        fail(f"refusing to overwrite {dest} — pass a different --output.", code=2)
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        storage.backup_to(db_path, dest)
    except storage.DatabaseFileError as exc:
        dest.unlink(missing_ok=True)
        fail_on_storage_refusal(exc)

    typer.echo(f"backed up database (schema {revision or 'unversioned'}) → {dest} ({_size(dest)})")


@app.command()
def vacuum() -> None:
    """Reclaim free space with VACUUM. Recommended monthly for long-running installs."""
    db_path = paths.db_path()
    # Same open-and-migrate path as every other command, so vacuum inherits the
    # schema guard rather than rebuilding a database this build can't describe.
    open_db().dispose()

    before = db_path.stat().st_size
    try:
        storage.vacuum(db_path)
    except storage.DatabaseFileError as exc:
        fail_on_storage_refusal(exc)
    after = db_path.stat().st_size

    reclaimed = before - after
    if reclaimed > 0:
        typer.echo(f"vacuumed database: {_bytes(before)} → {_bytes(after)} ({_bytes(reclaimed)} reclaimed)")
    else:
        typer.echo(f"vacuumed database: {_bytes(after)}, nothing to reclaim")


def _size(path: Path) -> str:
    return _bytes(path.stat().st_size)


def _bytes(count: int) -> str:
    kib = count / 1024
    return f"{kib:.0f} KiB" if kib < 1024 else f"{kib / 1024:.1f} MiB"
