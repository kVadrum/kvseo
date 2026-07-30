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

from kvseo.storage.db import DatabaseFileError, SchemaVersionError, open_engine


def fail(message: str, *, code: int) -> NoReturn:
    """Print an error line to stderr (red) and exit with ``code``."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def open_db() -> Engine:
    """``open_engine()`` with storage's two refusals mapped to exit 3.

    Storage raises; the CLI owns the exit-code contract (06 §2). 3 is
    "configuration error", and both refusals are that: ``SchemaVersionError``
    means the installed package and the database on disk disagree,
    ``DatabaseFileError`` means the configured path holds no usable database at
    all. Neither is the invocation being wrong, so neither is exit 2 — and
    neither should reach the user as a traceback, which is what exit 1 looks
    like from a CLI.
    """
    try:
        return open_engine()
    except (DatabaseFileError, SchemaVersionError) as exc:
        fail(str(exc), code=3)


def parse_audit_id(value: str) -> uuid.UUID:
    """Parse a CLI-supplied audit ID, or exit 2 with a clear message."""
    try:
        return uuid.UUID(value)
    except ValueError:
        fail(f"'{value}' is not a valid audit ID.", code=2)
