"""`kvseo connect psi` stores the API key (keyring mocked to an in-memory dict)."""

from __future__ import annotations

import json
from pathlib import Path

import keyring
import pytest
from keyring.errors import NoKeyringError
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.cli import connect as connect_cli
from kvseo.config.secrets import get_secret
from kvseo.connectors.base import ConnectorUnavailable
from kvseo.connectors.gsc import GscSite
from kvseo.connectors.gsc_auth import CLIENT_CONFIG_KEY, REFRESH_TOKEN_KEY

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


def test_connect_gsc_stores_the_grant_with_its_client_and_lists_properties(
    memory_keyring: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A refresh token only refreshes against the client that minted it, so the
    # client config is stored beside it rather than re-read from the env later.
    monkeypatch.setenv("KVSEO_GSC_CLIENT_ID", "cid")
    monkeypatch.setenv("KVSEO_GSC_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(connect_cli, "run_oauth_flow", lambda config, port=0: "rt-123")

    async def _props(refresh_token: str, config: dict[str, object]) -> list[GscSite]:
        assert refresh_token == "rt-123"
        return [GscSite(site_url="sc-domain:kemek.net", permission_level="siteOwner")]

    monkeypatch.setattr(connect_cli, "_properties", _props)
    result = runner.invoke(app, ["connect", "gsc"])

    assert result.exit_code == 0, result.output
    assert get_secret(REFRESH_TOKEN_KEY) == "rt-123"
    stored = json.loads(get_secret(CLIENT_CONFIG_KEY) or "{}")
    assert stored["installed"]["client_id"] == "cid"
    assert "sc-domain:kemek.net" in result.stdout


def test_connect_gsc_keeps_the_grant_when_listing_fails(
    memory_keyring: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KVSEO_GSC_CLIENT_ID", "cid")
    monkeypatch.setenv("KVSEO_GSC_CLIENT_SECRET", "csecret")
    monkeypatch.setattr(connect_cli, "run_oauth_flow", lambda config, port=0: "rt-123")

    async def _down(refresh_token: str, config: dict[str, object]) -> list[GscSite]:
        raise ConnectorUnavailable("GSC timed out on /sites")

    monkeypatch.setattr(connect_cli, "_properties", _down)
    result = runner.invoke(app, ["connect", "gsc"])

    assert result.exit_code == 0
    assert get_secret(REFRESH_TOKEN_KEY) == "rt-123"
    assert "timed out" in result.output
