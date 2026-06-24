"""Secret storage via the OS keyring (Keychain / Credential Manager / libsecret).

Config files reference secrets by name, never by value (02-architecture.md §7).
Service namespace is ``kvseo``; keys are ``<connector>:<field>`` — e.g.
``psi:api_key``, ``gsc:refresh_token``.
"""

from __future__ import annotations

import contextlib

import keyring
from keyring.errors import PasswordDeleteError

_SERVICE = "kvseo"


def get_secret(key: str) -> str | None:
    return keyring.get_password(_SERVICE, key)


def set_secret(key: str, value: str) -> None:
    keyring.set_password(_SERVICE, key, value)


def delete_secret(key: str) -> None:
    """Remove a secret; a no-op if it's already absent (idempotent)."""
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(_SERVICE, key)
