"""`kvseo connect psi` stores the API key (keyring mocked to an in-memory dict)."""

from __future__ import annotations

from pathlib import Path

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


def test_connect_gsc_auth_failure_exits_4(monkeypatch: pytest.MonkeyPatch) -> None:
    # With no OAuth client configured (no env vars, no --client-secrets), the GSC
    # flow raises ConnectorAuthError before any browser step. That's an auth
    # failure → exit 4 (06 §2 / §4.4), not the generic exit 1.
    monkeypatch.delenv("KVSEO_GSC_CLIENT_ID", raising=False)
    monkeypatch.delenv("KVSEO_GSC_CLIENT_SECRET", raising=False)
    result = runner.invoke(app, ["connect", "gsc"])
    assert result.exit_code == 4
    assert "No GSC OAuth client" in result.output


def test_connect_gsc_bad_client_secrets_path_exits_4(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A typo'd --client-secrets path is a config failure → exit 4 with a message,
    # not a raw FileNotFoundError traceback + generic exit 1.
    monkeypatch.delenv("KVSEO_GSC_CLIENT_ID", raising=False)
    monkeypatch.delenv("KVSEO_GSC_CLIENT_SECRET", raising=False)
    missing = tmp_path / "nope.json"
    result = runner.invoke(app, ["connect", "gsc", "--client-secrets", str(missing)])
    assert result.exit_code == 4
    assert "client secrets" in result.output.lower()


def test_connect_psi_reports_missing_keyring_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    # A headless box with no keyring backend must get an actionable error and a
    # non-zero exit, not an uncaught NoKeyringError traceback.
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise NoKeyringError("no recommended backend was available")

    monkeypatch.setattr(keyring, "set_password", _raise)
    result = runner.invoke(app, ["connect", "psi", "--api-key", "test-key-123"])
    assert result.exit_code == 3
    assert "no OS keyring backend" in result.output
