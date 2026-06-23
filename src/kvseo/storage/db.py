"""Engine creation and database initialisation.

WAL mode is always on (ADR-003): it lets the CLI and the future web UI share
one file with concurrent readers + a single writer. ``foreign_keys`` is enabled
per-connection (SQLite defaults it off).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.storage.models import Base, SchemaMeta

# Bumped when the physical schema changes; Alembic takes this over in the build.
SCHEMA_VERSION = "0"


def _register_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(db_path: Path) -> Engine:
    """Create an Engine for the SQLite file, creating its directory if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    _register_sqlite_pragmas(engine)
    return engine


def init_db(db_path: Path) -> Engine:
    """Create tables (if absent) and stamp the schema version. Idempotent."""
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        if session.get(SchemaMeta, "schema_version") is None:
            session.add(SchemaMeta(key="schema_version", value=SCHEMA_VERSION))
            session.commit()
    return engine
