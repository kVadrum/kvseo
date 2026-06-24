"""Secret storage via the OS keyring (Keychain / Credential Manager / libsecret).

Config files reference secrets by name, never by value (02-architecture.md §7).
Service namespace is ``kvseo``; keys are ``<connector>:<field>`` — e.g.
``psi:api_key``, ``gsc:refresh_token``.
"""

from __future__ import annotations

import contextlib

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

_SERVICE = "kvseo"


def get_secret(key: str) -> str | None:
    # A headless/CI box with no OS keyring backend raises NoKeyringError here.
    # Treat "no backend" as "no secret": callers fall back to env vars or run
    # keyless (e.g. PSI works without a key), instead of crashing the command.
    try:
        return keyring.get_password(_SERVICE, key)
    except KeyringError:
        return None


def set_secret(key: str, value: str) -> None:
    keyring.set_password(_SERVICE, key, value)


def delete_secret(key: str) -> None:
    """Remove a secret; a no-op if it's already absent (idempotent)."""
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(_SERVICE, key)
