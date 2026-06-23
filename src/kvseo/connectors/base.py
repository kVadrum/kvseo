"""Connector error taxonomy and the minimal connector protocol.

The error hierarchy is stable and already referenced by the audit engine's
graceful-degradation path (02-architecture.md §10). The full protocol —
capability ``meta``, typed return models, freshness + persistence patterns — is
specified in 03-connector-interfaces.md and implemented in the connector build.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class ConnectorError(Exception):
    """Base class for all connector failures."""


class ConnectorAuthError(ConnectorError):
    """Authentication or authorization with the upstream failed."""


class ConnectorUnavailable(ConnectorError):
    """The upstream could not be reached; the audit degrades gracefully."""


class ConnectorRateLimited(ConnectorError):
    """The upstream rejected the request because of rate limiting."""


@runtime_checkable
class Connector(Protocol):
    """Minimal read-only connector contract."""

    name: str

    def health_check(self) -> bool:
        """Return True if the upstream is reachable and credentials are valid."""
        ...
