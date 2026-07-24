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


class SecretStorageError(RuntimeError):
    """No OS keyring backend is available to *store* a secret.

    The read path (:func:`get_secret`) degrades to "no secret" on a backend-less
    box, but a failed *write* must be reported — silently dropping a credential
    would leave the connector broken with no signal.
    """


def get_secret(key: str) -> str | None:
    # A headless/CI box with no OS keyring backend raises NoKeyringError here.
    # Treat "no backend" as "no secret": callers fall back to env vars or run
    # keyless (e.g. PSI works without a key), instead of crashing the command.
    try:
        return keyring.get_password(_SERVICE, key)
    except KeyringError:
        return None


def set_secret(key: str, value: str) -> None:
    # Translate the backend's raw error into an actionable one — a headless / CI
    # box with no keyring raises here, and a traceback isn't a next step.
    try:
        keyring.set_password(_SERVICE, key, value)
    except KeyringError as exc:
        raise SecretStorageError(
            "couldn't store the credential — no OS keyring backend is available "
            "on this box. Install one (e.g. `pip install keyrings.alt`, or run "
            "gnome-keyring under a session D-Bus), then re-run this command."
        ) from exc


def delete_secret(key: str) -> None:
    """Remove a secret; a no-op if it's already absent (idempotent)."""
    with contextlib.suppress(PasswordDeleteError):
        keyring.delete_password(_SERVICE, key)
