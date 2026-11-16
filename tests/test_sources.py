"""The v0.2 sources at the CLI: `connect` proves credentials before storing
them, `connectors` reports what is stored, and an audit asks only the sources
that are connected and relevant. Keyring is an in-memory dict."""

from __future__ import annotations

from typing import Any

import keyring
import pytest
from sqlalchemy.engine import Engine
from typer.testing import CliRunner

from kvseo.cli import _sources, app
from kvseo.cli import connect as connect_cli
from kvseo.config.secrets import get_secret
from kvseo.connectors.base import ConnectorAuthError, ConnectorUnavailable
from kvseo.connectors.dataforseo import DataForSeoConnector
from kvseo.connectors.openseo import OpenSeoConnector, OpenSeoProject
from kvseo.connectors.serpbear import SerpBearConnector

runner = CliRunner()

_ENV = ("SERPBEAR_URL", "SERPBEAR_API_KEY", "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "OPENSEO_API_KEY", "OPENSEO_URL")


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    data: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, k, v: data.__setitem__((s, k), v))
    monkeypatch.setattr(keyring, "get_password", lambda s, k: data.get((s, k)))
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    return data


def _returns(value: Any) -> Any:
    async def method(self: object, *args: object, **kwargs: object) -> Any:
        return value

    return method


def _raises(exc: Exception) -> Any:
    async def method(self: object, *args: object, **kwargs: object) -> Any:
        raise exc

    return method


def test_connect_serpbear_stores_after_a_passing_check(
    store: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SerpBearConnector, "domains", _returns(["kemek.net"]))
    result = runner.invoke(app, ["connect", "serpbear", "--url", "https://sb.example", "--token", "k"])
    assert result.exit_code == 0, result.output
    assert "kemek.net" in result.stdout
    assert (get_secret(_sources.SERPBEAR_URL), get_secret(_sources.SERPBEAR_TOKEN)) == ("https://sb.example", "k")


@pytest.mark.parametrize(("exc", "code"), [(ConnectorAuthError("bad key"), 4), (ConnectorUnavailable("down"), 5)])
def test_connect_never_stores_credentials_that_failed_the_check(
    store: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch, exc: Exception, code: int
) -> None:
    monkeypatch.setattr(DataForSeoConnector, "health_check", _raises(exc))
    result = runner.invoke(app, ["connect", "dataforseo", "--login", "a@b.c", "--password", "pw"])
    assert result.exit_code == code
    assert store == {}


def test_connect_openseo_lists_projects(store: dict[tuple[str, str], str], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OpenSeoConnector, "projects", _returns([OpenSeoProject(id="p", name="KeMeK", domain=None)]))
    result = runner.invoke(app, ["connect", "openseo", "--token", "oseo_x"])
    assert result.exit_code == 0, result.output
    assert "KeMeK" in result.stdout
    assert get_secret(_sources.OPENSEO_URL) == connect_cli.HOSTED_URL


def test_connectors_lists_what_is_stored(store: dict[tuple[str, str], str]) -> None:
    store[("kvseo", _sources.SERPBEAR_URL)] = "https://sb.example"
    store[("kvseo", _sources.SERPBEAR_TOKEN)] = "k"
    result = runner.invoke(app, ["connectors"])
    assert result.exit_code == 0
    lines = {line.split()[1]: line for line in result.stdout.splitlines()[1:]}
    assert lines["serpbear"].lstrip().startswith("✓") and "https://sb.example" in lines["serpbear"]
    assert "(not configured)" in lines["dataforseo"]


def test_audit_asks_only_connected_and_relevant_sources(
    store: dict[tuple[str, str], str], audit_engine: Engine
) -> None:
    assert _sources.audit_pulls(audit_engine, "seo audit") == {}

    store[("kvseo", _sources.OPENSEO_TOKEN)] = "oseo_x"
    assert set(_sources.audit_pulls(audit_engine, "seo audit")) == {"openseo"}
    assert _sources.audit_pulls(audit_engine, None) == {}  # nothing to measure without a keyword

    # Both keyword sources connected: one call, to DataForSEO, the direct source.
    store[("kvseo", _sources.DATAFORSEO_LOGIN)] = "a@b.c"
    store[("kvseo", _sources.DATAFORSEO_PASSWORD)] = "pw"
    store[("kvseo", _sources.SERPBEAR_URL)] = "https://sb.example"
    store[("kvseo", _sources.SERPBEAR_TOKEN)] = "k"
    assert set(_sources.audit_pulls(audit_engine, "seo audit")) == {"serpbear", "dataforseo"}
