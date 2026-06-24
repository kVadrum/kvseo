"""GSC OAuth client-config resolution (the only part testable without live Google)."""

from __future__ import annotations

import pytest

from kvseo.connectors.base import ConnectorAuthError
from kvseo.connectors.gsc_auth import _client_config


def test_client_config_errors_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KVSEO_GSC_CLIENT_ID", raising=False)
    monkeypatch.delenv("KVSEO_GSC_CLIENT_SECRET", raising=False)
    with pytest.raises(ConnectorAuthError):
        _client_config()


def test_client_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KVSEO_GSC_CLIENT_ID", "cid")
    monkeypatch.setenv("KVSEO_GSC_CLIENT_SECRET", "secret")
    config = _client_config()
    assert config["installed"]["client_id"] == "cid"
    assert config["installed"]["token_uri"].startswith("https://")
