"""Connector error taxonomy + metadata model."""

from __future__ import annotations

from kvseo.connectors.base import (
    ConnectorAuthError,
    ConnectorError,
    ConnectorMeta,
    ConnectorRateLimited,
    ConnectorUnavailable,
)


def test_rate_limited_carries_retry_after() -> None:
    err = ConnectorRateLimited(30, "slow down")
    assert err.retry_after == 30
    assert isinstance(err, ConnectorError)


def test_error_hierarchy() -> None:
    assert issubclass(ConnectorAuthError, ConnectorError)
    assert issubclass(ConnectorUnavailable, ConnectorError)
    assert issubclass(ConnectorRateLimited, ConnectorError)


def test_meta_model() -> None:
    meta = ConnectorMeta(name="psi", version="0.1.0", capabilities=["cwv", "lighthouse"])
    assert meta.name == "psi"
    assert "cwv" in meta.capabilities
