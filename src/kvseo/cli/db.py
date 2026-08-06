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
    except storage.StorageRefusal as exc:
        fail_on_storage_refusal(exc)

    # open_db() is the migration: it carries both the upgrade and the exit-3
    # mapping, so this command stays a reporting wrapper around the shared path.
    # Once it returns, the database is at head by definition — no second probe.
    open_db().dispose()

    if before == storage.HEAD_REVISION:
        typer.echo(f"database already at schema {storage.HEAD_REVISION}: {db_path}")
    else:
        typer.echo(f"migrated database {before or '(unversioned)'} → {storage.HEAD_REVISION}: {db_path}")


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
    except storage.StorageRefusal as exc:
        fail_on_storage_refusal(exc)

    # An explicit --output is a path the user chose, so a collision there is
    # theirs to resolve. A default name is ours, and it must not fail just
    # because the last backup was in the same second.
    if output is not None:
        dest = output
        if dest.exists():
            fail(f"refusing to overwrite {dest} — pass a different --output.", code=2)
        # exist_ok tolerates an existing *directory*, not an existing file, so a
        # --output under a regular file raises FileExistsError. That is a bad
        # flag value (06 §2 exit 2), not a crash.
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            fail(f"cannot create the directory for {dest} ({exc.strerror}) — pass a different --output.", code=2)
    else:
        dest = _free_default_path(paths.data_dir() / "backups")

    try:
        storage.backup_to(db_path, dest)
    except storage.StorageRefusal as exc:
        fail_on_storage_refusal(exc)

    typer.echo(f"backed up database (schema {revision or 'unversioned'}) → {dest} ({_size(dest)})")


@app.command()
def vacuum() -> None:
    """Reclaim free space with VACUUM. Recommended monthly for long-running installs."""
    db_path = paths.db_path()
    # Same open-and-migrate path as every other command, so vacuum inherits the
    # schema guard rather than rebuilding a database this build can't describe.
    open_db().dispose()

    before = storage.disk_footprint(db_path)
    try:
        landed = storage.vacuum(db_path)
    except storage.StorageRefusal as exc:
        fail_on_storage_refusal(exc)

    if not landed:
        # The rebuild is committed; it just cannot be folded into the main file
        # while another connection is attached. SQLite does that itself when the
        # connection goes, so this is success — and saying otherwise sent the
        # user to repeat a completed rebuild for nothing.
        typer.echo(
            "vacuumed database: rebuilt, but another connection is attached — "
            "the space returns to the file when it disconnects. Nothing to redo."
        )
        return

    after = storage.disk_footprint(db_path)
    reclaimed = before - after
    if reclaimed > 0:
        typer.echo(f"vacuumed database: {_bytes(before)} → {_bytes(after)} ({_bytes(reclaimed)} reclaimed)")
    else:
        typer.echo(f"vacuumed database: {_bytes(after)}, nothing to reclaim")


def _free_default_path(backups_dir: Path) -> Path:
    """A free ``kvseo-<ts>.db`` in ``backups_dir``, suffixed only if it must be.

    06 §4.10.2 fixes the default name at one-second resolution, so two backups
    in the same second collide. Keeping the documented shape and falling back to
    ``-2``, ``-3``, … costs nothing in the normal case and stops a scripted
    "back up, then back up again" from failing on a name we chose ourselves.
    """
    try:
        backups_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Not exit 2: the user did not choose this path, so the environment is
        # what is wrong (06 §2 exit 3).
        fail(f"cannot create the backups directory {backups_dir} ({exc.strerror}).", code=3)
    stamp = datetime.now(UTC).strftime(_BACKUP_TS)
    dest = backups_dir / f"kvseo-{stamp}.db"
    nth = 2
    while dest.exists():
        dest = backups_dir / f"kvseo-{stamp}-{nth}.db"
        nth += 1
    return dest


def _size(path: Path) -> str:
    return _bytes(path.stat().st_size)


def _bytes(count: int) -> str:
    if count < 1024:
        return f"{count} B"  # a sub-KiB reclaim must not print as "0 KiB reclaimed"
    kib = count / 1024
    return f"{kib:.0f} KiB" if kib < 1024 else f"{kib / 1024:.1f} MiB"
