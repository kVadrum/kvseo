"""Secret storage degrades gracefully when no OS keyring backend is present."""

from __future__ import annotations

import pytest
from keyring.errors import NoKeyringError

from kvseo.config import secrets


def test_get_secret_returns_none_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # A headless / CI box with no keyring backend raises NoKeyringError. get_secret
    # must swallow it and return None so callers fall back to env vars / keyless
    # operation instead of crashing the command (e.g. `kvseo audit` with CWV on).
    def _raise(*_args: object, **_kwargs: object) -> str | None:
        raise NoKeyringError("no recommended backend was available")

    monkeypatch.setattr(secrets.keyring, "get_password", _raise)
    assert secrets.get_secret("psi:api_key") is None


def test_set_secret_raises_friendly_error_without_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # The write path can't swallow the failure (that would silently drop the
    # credential); it must raise a typed, actionable error instead of a raw
    # NoKeyringError traceback so `kvseo connect` can report a next step.
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise NoKeyringError("no recommended backend was available")

    monkeypatch.setattr(secrets.keyring, "set_password", _raise)
    with pytest.raises(secrets.SecretStorageError):
        secrets.set_secret("psi:api_key", "value")
