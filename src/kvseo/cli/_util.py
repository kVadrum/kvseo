"""Shared CLI helpers used across kvseo command modules.

Small building blocks the command modules reach for — an error-exit helper and
the audit-ID parser — kept here so their message + exit-code contract has one
home instead of a copy per command.
"""

from __future__ import annotations

import uuid
from typing import NoReturn

import typer


def fail(message: str, *, code: int) -> NoReturn:
    """Print an error line to stderr (red) and exit with ``code``."""
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def parse_audit_id(value: str) -> uuid.UUID:
    """Parse a CLI-supplied audit ID, or exit 2 with a clear message."""
    try:
        return uuid.UUID(value)
    except ValueError:
        fail(f"'{value}' is not a valid audit ID.", code=2)
