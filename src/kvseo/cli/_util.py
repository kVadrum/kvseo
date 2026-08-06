"""Shared CLI helpers used across kvseo command modules.

Small building blocks the command modules reach for — an error-exit helper and
the audit-ID parser — kept here so their message + exit-code contract has one
home instead of a copy per command.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

import typer
from sqlalchemy.engine import Engine

from kvseo.storage.db import DatabaseBusyError, StorageRefusal, open_engine


def fail(message: str, *, code: int) -> NoReturn:
    """Print an error line to stderr (red) and exit with ``code``."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def open_db() -> Engine:
    """``open_engine()`` with storage's refusals mapped to their exit codes.

    ``open_engine`` migrates, so every refusal storage raises is reachable from
    here — including lock contention, which needs both a concurrent writer and
    migrations actually pending.
    """
    try:
        return open_engine()
    except StorageRefusal as exc:
        fail_on_storage_refusal(exc)


def fail_on_storage_refusal(exc: StorageRefusal) -> NoReturn:
    """Map a storage-layer refusal to its exit code — the CLI's half of 06 §2.

    **3** for the fatal refusals: the environment is wrong, not the
    invocation, so not exit 2. ``SchemaVersionError`` means the package and the
    database disagree; ``DatabaseFileError`` means the path holds no usable
    database at all.

    **1** for contention, because 06 §2 has no lock-contention code and says not
    to add one ad-hoc — so it lands in "general error (caught exception)", where
    *caught* is the operative word. It is also the honest code: unlike the other
    two, retrying unchanged is expected to work.

    Kept separate from ``open_db`` because ``kvseo db backup`` and ``db vacuum``
    reach the database outside it — backup deliberately skips the
    migrate-on-open, and vacuum's own SQLite work happens after it. One home for
    *which* code, several callers for *when*.
    """
    fail(str(exc), code=1 if isinstance(exc, DatabaseBusyError) else 3)


def parse_audit_id(value: str) -> uuid.UUID:
    """Parse a CLI-supplied audit ID, or exit 2 with a clear message."""
    try:
        return uuid.UUID(value)
    except ValueError:
        fail(f"'{value}' is not a valid audit ID.", code=2)
