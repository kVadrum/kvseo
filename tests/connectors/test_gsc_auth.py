"""GSC OAuth: client-config resolution and the token refresh (the browser flow needs live Google)."""

from __future__ import annotations

import httpx
import pytest

from kvseo.connectors.base import ConnectorAuthError, ConnectorUnavailable
from kvseo.connectors.gsc_auth import client_config, refresh_access_token


def test_client_config_errors_without_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KVSEO_GSC_CLIENT_ID", raising=False)
    monkeypatch.delenv("KVSEO_GSC_CLIENT_SECRET", raising=False)
    with pytest.raises(ConnectorAuthError):
        client_config()


def test_client_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KVSEO_GSC_CLIENT_ID", "cid")
    monkeypatch.setenv("KVSEO_GSC_CLIENT_SECRET", "secret")
    config = client_config()
    assert config["installed"]["client_id"] == "cid"
    assert config["installed"]["token_uri"].startswith("https://")


_CONFIG = {
    "installed": {
        "client_id": "cid",
        "client_secret": "csecret",
        "token_uri": "https://oauth2.example/token",
    }
}


def _token_client(status: int, body: object, seen: list[httpx.Request] | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refresh_exchanges_the_grant_with_the_stored_client() -> None:
    seen: list[httpx.Request] = []
    client = _token_client(200, {"access_token": "fresh", "expires_in": 3599}, seen)
    assert await refresh_access_token("rt", _CONFIG, client=client) == "fresh"
    assert str(seen[0].url) == "https://oauth2.example/token"
    form = dict(httpx.QueryParams(seen[0].content.decode()))
    assert form == {
        "grant_type": "refresh_token",
        "refresh_token": "rt",
        "client_id": "cid",
        "client_secret": "csecret",
    }


async def test_revoked_grant_says_reconnect() -> None:
    client = _token_client(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
    with pytest.raises(ConnectorAuthError, match="revoked or has expired — re-run `kvseo connect gsc`"):
        await refresh_access_token("rt", _CONFIG, client=client)


async def test_token_endpoint_outage_is_unavailable_not_auth() -> None:
    # Reconnecting would not fix a 5xx, so it must not say to.
    with pytest.raises(ConnectorUnavailable):
        await refresh_access_token("rt", _CONFIG, client=_token_client(503, {}))

    def boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    with pytest.raises(ConnectorUnavailable):
        await refresh_access_token(
            "rt", _CONFIG, client=httpx.AsyncClient(transport=httpx.MockTransport(boom))
        )


async def test_config_without_a_client_id_is_an_auth_error() -> None:
    with pytest.raises(ConnectorAuthError, match="client_id"):
        await refresh_access_token("rt", {"installed": {}}, client=_token_client(200, {}))
