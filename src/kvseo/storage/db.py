"""Engine creation, WAL setup, and Alembic-driven migration (ADR-003).

WAL mode is always on: it lets the CLI and the future web UI share one file
with concurrent readers + a single writer. ``foreign_keys`` is enabled
per-connection (SQLite defaults it off, and the schema relies on ON DELETE
cascades). Schema creation goes through Alembic — never ``create_all`` — so the
database's ``alembic_version`` always reflects a known migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError

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
    """
    from alembic import command  # deferred: ~100ms to import, and only migrate()

    check_schema_version(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(db_path), "head")
    engine = get_engine(db_path)
    engine.connect().close()
    engine.dispose()


def stored_revision(db_path: Path) -> str | None:
    """The database's current ``alembic_version``, or None if there isn't one.

    Raw SQL rather than Alembic on purpose: this runs ahead of every database
    command, and reaching for Alembic here would undo the cold-start deferral
    that ``migrate()`` goes out of its way to preserve. A missing file or a
    missing ``alembic_version`` table both read as None — an unmigrated
    database is not a mismatch, it is one ``migrate()`` away from correct.
    """
    if not db_path.exists():
        return None
    engine = get_engine(db_path)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except DatabaseError:
        return None
    finally:
        engine.dispose()
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
