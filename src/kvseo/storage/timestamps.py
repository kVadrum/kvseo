"""Canonical SQLite timestamp format + parse/format helpers.

Timestamp columns (``fetched_at``, ``created_at``, ``completed_at``) are stored
as TEXT in the SQLite-native ``YYYY-MM-DD HH:MM:SS`` shape (what ``datetime('now')``
emits). That format is a *wire contract*: every writer's ``strftime`` must match
every reader's ``strptime`` exactly, so it lives here once rather than being
redeclared per connector/module.
"""

from __future__ import annotations

from datetime import UTC, datetime

SQLITE_TS = "%Y-%m-%d %H:%M:%S"


def now_str() -> str:
    """Current UTC time, formatted for a SQLite TEXT timestamp column."""
    return datetime.now(UTC).strftime(SQLITE_TS)


def parse_ts(value: str) -> datetime:
    """Parse a stored SQLite timestamp string into a UTC-aware ``datetime``."""
    return datetime.strptime(value, SQLITE_TS).replace(tzinfo=UTC)
