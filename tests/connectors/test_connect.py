"""`kvseo connect psi` stores the API key (keyring mocked to an in-memory dict)."""

from __future__ import annotations

import keyring
import pytest
from keyring.errors import NoKeyringError
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.config.secrets import get_secret

runner = CliRunner()


@pytest.fixture
def memory_keyring(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    store: dict[tuple[str, str], str] = {}

    def _set(service: str, key: str, value: str) -> None:
        store[(service, key)] = value

    def _get(service: str, key: str) -> str | None:
        return store.get((service, key))

    monkeypatch.setattr(keyring, "set_password", _set)
    monkeypatch.setattr(keyring, "get_password", _get)
    return store


def test_connect_psi_stores_key(memory_keyring: dict[tuple[str, str], str]) -> None:
    result = runner.invoke(app, ["connect", "psi", "--api-key", "test-key-123"])
    assert result.exit_code == 0
    assert get_secret("psi:api_key") == "test-key-123"


def test_connect_psi_without_key_is_informational(
    memory_keyring: dict[tuple[str, str], str],
) -> None:
    result = runner.invoke(app, ["connect", "psi"])
    assert result.exit_code == 0
    assert "without a key" in result.stdout
    assert get_secret("psi:api_key") is None


def test_connect_psi_reports_missing_keyring_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # A headless box with no keyring backend must get an actionable error and a
    # non-zero exit, not an uncaught NoKeyringError traceback.
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise NoKeyringError("no recommended backend was available")

    monkeypatch.setattr(keyring, "set_password", _raise)
    result = runner.invoke(app, ["connect", "psi", "--api-key", "test-key-123"])
    assert result.exit_code == 3
    assert "no OS keyring backend" in result.output
