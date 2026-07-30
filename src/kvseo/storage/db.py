"""Engine creation, WAL setup, and Alembic-driven migration (ADR-003).

WAL mode is always on: it lets the CLI and the future web UI share one file
with concurrent readers + a single writer. ``foreign_keys`` is enabled
per-connection (SQLite defaults it off, and the schema relies on ON DELETE
cascades). Schema creation goes through Alembic — never ``create_all`` — so the
database's ``alembic_version`` always reflects a known migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from kvseo import __version__
from kvseo.config import paths

if TYPE_CHECKING:
    from alembic.config import Config

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Every migration revision this package ships, oldest first. Held as a literal
# so the startup check below stays free of the ~100ms alembic import that
# migrate() is careful to defer; `test_known_revisions_match_alembic` pins the
# tuple against Alembic's own ScriptDirectory, so adding a migration without
# updating it fails CI rather than shipping a wrong answer.
KNOWN_REVISIONS = ("0001",)
HEAD_REVISION = KNOWN_REVISIONS[-1]


class SchemaVersionError(RuntimeError):
    """The database was written by a newer kvseo than the one running."""


class DatabaseFileError(RuntimeError):
    """The path configured for the database does not hold a usable one."""


class DatabaseBusyError(RuntimeError):
    """Another connection holds a lock this operation needs to proceed."""


# SQLite primary result codes, split by what the user has to do about them
# (https://sqlite.org/rescode.html). Extended codes carry the primary code in
# their low byte — SQLITE_CORRUPT_VTAB is 11 | 1 << 8 — so mask before comparing.
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_SQLITE_CORRUPT = 11
_SQLITE_CANTOPEN = 14
_SQLITE_NOTADB = 26
# Fatal: the file is not something we can work with, and retrying won't help.
_FILE_FAULT_CODES = frozenset({_SQLITE_CORRUPT, _SQLITE_CANTOPEN, _SQLITE_NOTADB})
# Transient: someone else holds the lock. Retrying later is the whole fix.
_BUSY_CODES = frozenset({_SQLITE_BUSY, _SQLITE_LOCKED})


def _primary_code(exc: BaseException) -> int | None:
    """``exc``'s SQLite primary result code, or None if it isn't a SQLite error.

    Both the bare driver error and SQLAlchemy's wrapper are accepted, since the
    same file is reached through stdlib sqlite3 and through an Engine.
    """
    driver_exc = getattr(exc, "orig", exc)
    if not isinstance(driver_exc, sqlite3.Error):
        return None
    code = getattr(driver_exc, "sqlite_errorcode", None)
    return code & 0xFF if isinstance(code, int) else None


def _file_fault_code(exc: BaseException) -> int | None:
    """The primary result code, when ``exc`` blames the database file.

    None for everything else, which is the whole point: a missing table on an
    unmigrated database (SQLITE_ERROR), a locked file (SQLITE_BUSY), and a real
    bug in a migration are all ``DatabaseError``s too, and each must keep the
    handling it has today. Only these three codes mean "there is no database
    here to work with" — the one class no retry and no migration can fix.
    """
    code = _primary_code(exc)
    return code if code in _FILE_FAULT_CODES else None


def _reject_unusable_file(db_path: Path, exc: BaseException) -> None:
    """Raise ``DatabaseFileError`` if ``exc`` blames the file; return otherwise.

    Callers pair this with their own ``raise`` so an unrecognised failure keeps
    its original type and traceback — this only renames the one class of error
    a user can fix, into a message that says how (06 §2 maps it to exit 3).
    """
    code = _file_fault_code(exc)
    if code is None:
        return
    driver_exc: BaseException = getattr(exc, "orig", exc)
    if code == _SQLITE_CANTOPEN:
        message = (
            f"cannot open a database at {db_path} (SQLite: {driver_exc}). Check that the path is a writable "
            f"file and not a directory, or point $KVSEO_DATA_DIR at a different directory."
        )
    else:
        message = _not_a_database_message(db_path, f"SQLite: {driver_exc}")
    raise DatabaseFileError(message) from exc


def _not_a_database_message(db_path: Path, detail: str) -> str:
    return (
        f"the file at {db_path} is not a usable kvseo database ({detail}). Move or delete it and re-run "
        f"`kvseo init`. If it held audit history you need, restore a `kvseo db backup` instead."
    )


def _busy_message(needs: str) -> str:
    return (
        f"the database is in use by another process, and {needs} needs exclusive access. "
        f"Close any other running kvseo command and try again."
    )


def _reject_busy(exc: BaseException, *, needs: str) -> None:
    """Raise ``DatabaseBusyError`` if ``exc`` is lock contention; return otherwise.

    Called from the operations that need a lock they cannot get by waiting their
    turn inside a normal transaction: ``VACUUM``, and ``migrate`` when it has DDL
    to run. A read, or a migrate with nothing to do, does not contend at all —
    WAL gives readers a free pass — so those keep raising whatever they raise.
    """
    if _primary_code(exc) not in _BUSY_CODES:
        return
    raise DatabaseBusyError(_busy_message(needs)) from exc


# The first 16 bytes of every SQLite database file (https://sqlite.org/fileformat.html).
_SQLITE_MAGIC = b"SQLite format 3\x00"


def _reject_non_sqlite_file(db_path: Path) -> None:
    """Refuse a non-empty file whose header is not SQLite's, before any writer runs.

    SQLite treats a *very* small file as an empty database and initialises over
    it, so a stray 1-byte file at the database path was silently destroyed by
    the first command that opened it — measured: 1 byte overwritten, 50 bytes
    and up already refused with SQLITE_NOTADB. Reading the magic closes the gap
    below that threshold, and it is cheaper than the probe it precedes.

    A zero-byte file reads as empty and is left to SQLite, which is correct:
    that is the shape ``touch`` leaves behind and an empty file holds nothing to
    lose.
    """
    try:
        with db_path.open("rb") as fh:
            header = fh.read(len(_SQLITE_MAGIC))
    except OSError:
        return  # Let the driver raise the authoritative error for an unreadable path.
    if header and header != _SQLITE_MAGIC:
        raise DatabaseFileError(_not_a_database_message(db_path, "no SQLite file header"))


def _register_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(db_path: Path) -> Engine:
    """Create an Engine for the SQLite file (WAL + FK pragmas on connect)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _register_sqlite_pragmas(engine)
    return engine


def _alembic_config(db_path: Path) -> Config:
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def migrate(db_path: Path) -> None:
    """Upgrade the database to the latest schema (``alembic upgrade head``).

    Idempotent. Also forces WAL mode on the file: Alembic's own engine doesn't
    carry our pragma listener, so we open one connection through ``get_engine``
    afterwards (WAL is a persistent, file-level mode in SQLite).

    Raises ``SchemaVersionError`` if the database is *ahead* of this package —
    the guard sits here rather than in ``open_engine()`` because this is the
    single point where every entry path touches an existing database, ``kvseo
    init`` included. Without it, an unknown revision surfaces as a raw Alembic
    "can't locate revision" traceback instead of an actionable message.

    Raises ``DatabaseFileError`` if the path holds something that is not a
    database. ``check_schema_version`` catches the common case on its cheap
    stdlib probe; the wrap here covers corruption deeper in the file, which is
    only reachable once Alembic starts reading pages.

    Raises ``DatabaseBusyError`` if another connection holds the write lock
    *and* there are migrations to apply. Reaching that needs both halves: with
    the database already at head this is a read and contends with nothing, which
    is why the busy path is easy to miss when testing on a current database.
    """
    check_schema_version(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    from alembic import command  # deferred: ~100ms to import, and only migrate()

    try:
        command.upgrade(_alembic_config(db_path), "head")
        engine = get_engine(db_path)
        try:
            engine.connect().close()
        finally:
            engine.dispose()
    except Exception as exc:
        _reject_unusable_file(db_path, exc)
        _reject_busy(exc, needs="applying migrations")
        raise


def stored_revision(db_path: Path) -> str | None:
    """The database's current ``alembic_version``, or None if there isn't one.

    Stdlib sqlite3 rather than Alembic or an Engine on purpose: this probe
    runs ahead of every database command, so it must stay free of the ~100ms
    alembic import ``migrate()`` defers — and Engine construction with its
    WAL/FK pragmas is machinery a one-row read doesn't need. A missing file and
    a missing ``alembic_version`` table both read as None — an unmigrated
    database is not a mismatch, it is one ``migrate()`` away from correct.

    A path that is not a database at all is the one failure that does *not*
    read as None: it raises ``DatabaseFileError``. Migrating cannot fix it, and
    this probe is the cheapest place to say so — before the alembic import,
    where the alternative is a SQLAlchemy traceback out of ``upgrade``.
    """
    if not db_path.exists():
        return None
    _reject_non_sqlite_file(db_path)
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        _reject_unusable_file(db_path, exc)
        return None
    try:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.Error as exc:
        _reject_unusable_file(db_path, exc)
        return None
    finally:
        conn.close()
    return str(row[0]) if row else None


def check_schema_version(db_path: Path) -> None:
    """Refuse to operate on a database newer than this package (07 §4).

    Only the *ahead* direction needs a guard. A database behind the package
    self-heals, because ``open_engine()`` runs ``migrate()`` on every command —
    that is the "pip upgraded but db didn't" case, and it never reaches a user.
    The reverse is the one that bites: a database written by a newer kvseo
    carries a revision this build has never heard of, so migrating or querying
    it means operating blind on a schema we cannot describe. Downgrading the
    package is the usual cause (a shared data dir across two installs is the
    other), and neither is fixable by migrating forward.
    """
    stored = stored_revision(db_path)
    if stored is None or stored in KNOWN_REVISIONS:
        return
    raise SchemaVersionError(
        f"database schema '{stored}' is newer than this kvseo ({__version__}) "
        f"understands (latest known: '{HEAD_REVISION}'). Upgrade kvseo with "
        f"`pip install -U kvseo`, or point $KVSEO_DATA_DIR at a different database."
    )


def backup_to(db_path: Path, dest: Path) -> None:
    """Copy the database to ``dest`` with SQLite's online backup API (07 §5).

    Safe to run with the database in use — the API copies a consistent snapshot
    page by page and restarts if a writer commits mid-copy, which a filesystem
    ``cp`` of a live WAL database cannot promise. No migration and no
    schema-version guard on purpose: this captures the file as it stands, and
    the two moments you most want a copy are right before an upgrade and right
    after discovering the installed package is too old for it.

    Failures are attributed by *where* they arise, because the same result code
    means opposite things on either side of the copy. Opening ``dest`` can only
    be about ``dest``; once it is open, SQLITE_CANTOPEN belongs to the source
    (``sqlite3.connect`` is lazy, so ``backup()`` is where the source is first
    actually read), while a full or failing disk belongs to ``dest``.
    """
    try:
        source = sqlite3.connect(db_path)
    except sqlite3.Error as exc:
        # Eager failure here means the source path is not openable at all — a
        # directory, say. The lazy-connect reasoning below does not cover it.
        _reject_unusable_file(db_path, exc)
        raise
    try:
        try:
            target = sqlite3.connect(dest)
        except sqlite3.Error as exc:
            raise DatabaseFileError(_backup_dest_message(dest, exc)) from exc
        try:
            source.backup(target)
        except sqlite3.Error as exc:
            _reject_unusable_file(db_path, exc)
            raise DatabaseFileError(_backup_dest_message(dest, exc)) from exc
        finally:
            target.close()
    finally:
        source.close()


def _backup_dest_message(dest: Path, exc: BaseException) -> str:
    return (
        f"could not write the backup to {dest} (SQLite: {exc}). Check that the directory exists, is "
        f"writable, and has room — or pass a different --output."
    )


def _checkpoint_or_busy(conn: sqlite3.Connection) -> None:
    """Checkpoint the WAL into the main file, or raise if someone is blocking it.

    ``PRAGMA wal_checkpoint`` reports failure in its result row (busy, log
    frames, checkpointed frames) instead of raising, so the row must be read.
    """
    busy, _log_frames, _checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if busy:
        raise DatabaseBusyError(_busy_message("VACUUM"))


def vacuum(db_path: Path) -> None:
    """Rebuild the database, reclaiming free pages (06 §4.10.3).

    ``isolation_level=None`` because VACUUM cannot run inside a transaction and
    stdlib sqlite3 otherwise opens one implicitly.

    Raises ``DatabaseBusyError`` when another connection holds the database —
    including a mere *reader*, which is not SQLite's rule but is the honest one
    here. VACUUM does succeed against a reader; it just writes the rebuilt
    database into the WAL, and nothing can move that into the main file while the
    reader's snapshot stands. The caller measures the main file, so the result is
    a command reporting that it reclaimed nothing having in fact reclaimed
    megabytes.

    The checkpoint *after* VACUUM is what catches it. The one before cannot: the
    WAL is typically empty at that point, so it returns "not busy" no matter who
    else is attached. Neither raises on failure — the busy flag is in the return
    row, which is why both are checked rather than executed and forgotten.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        # Before: so the caller's "size before" sees a complete main file.
        _checkpoint_or_busy(conn)
        conn.execute("VACUUM")
        # After: this is the one that has to land, or nothing visibly shrank.
        _checkpoint_or_busy(conn)
    except sqlite3.Error as exc:
        _reject_unusable_file(db_path, exc)
        _reject_busy(exc, needs="VACUUM")
        raise
    finally:
        conn.close()


def open_engine() -> Engine:
    """Open the app database at its default path, migrated to head first.

    The one call every CLI command uses to get a ready-to-query engine — it
    folds ``db_path() → migrate() → get_engine()`` into one place. ``migrate()``
    carries the schema-version guard, so it gates exactly the commands that
    touch the database: ``--version`` and ``--help`` stay working (and stay
    fast) on a machine whose database is unusable.
    """
    db_path = paths.db_path()
    migrate(db_path)
    return get_engine(db_path)
