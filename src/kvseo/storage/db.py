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

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from kvseo.config import paths

if TYPE_CHECKING:
    from alembic.config import Config

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


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
    """
    from alembic import command  # deferred: ~100ms to import, and only migrate()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(_alembic_config(db_path), "head")
    engine = get_engine(db_path)
    engine.connect().close()
    engine.dispose()


def open_engine() -> Engine:
    """Open the app database at its default path, migrated to head first.

    The one call every CLI command uses to get a ready-to-query engine — it
    folds ``db_path() → migrate() → get_engine()`` into one place.
    """
    db_path = paths.db_path()
    migrate(db_path)
    return get_engine(db_path)
