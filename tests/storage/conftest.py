"""Shared fixtures for the storage-layer tests.

Callable-returning fixtures, per the top-level conftest's ``seed`` pattern:
``tests/`` is not a package, so helpers shared across files travel as fixtures
rather than imports.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text

from kvseo.storage.db import get_engine


@pytest.fixture
def set_revision() -> Callable[[Path, str], None]:
    """Force the ``alembic_version`` stamp — the "written by another build" setup."""

    def _set(db: Path, revision: str) -> None:
        engine = get_engine(db)
        with engine.begin() as conn:
            conn.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": revision})
        engine.dispose()

    return _set


@pytest.fixture
def tables() -> Callable[[Path], set[str]]:
    """The database's table inventory, straight from ``sqlite_master``."""

    def _tables(db: Path) -> set[str]:
        conn = sqlite3.connect(db)
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    return _tables


@pytest.fixture
def unversioned_db() -> Callable[[Path], Path]:
    """A valid SQLite file with none of our tables — the "unversioned" shape."""

    def _make(db: Path) -> Path:
        engine = get_engine(db)
        engine.connect().close()
        engine.dispose()
        return db

    return _make
