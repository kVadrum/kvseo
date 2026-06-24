"""Connector protocol, error taxonomy, and metadata (03-connector-interfaces.md §1).

Connectors don't share a base class — their capabilities differ — so they
implement this marker protocol and raise this error hierarchy instead. Every
connector is async (httpx), returns pydantic models, persists on success, and
contains no business logic (interpreting the data is the advisor's job).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ConnectorError(Exception):
    """Base class for all connector failures."""


class ConnectorUnavailable(ConnectorError):
    """Upstream unreachable or returned 5xx — the audit continues without it."""


class ConnectorAuthError(ConnectorError):
    """Auth missing, expired, or invalid — user action required."""


class ConnectorRateLimited(ConnectorError):
    """Upstream rate limit; ``retry_after`` is the seconds to wait before retry."""

    def __init__(self, retry_after: int, *args: object) -> None:
        super().__init__(*args)
        self.retry_after = retry_after


class ConnectorMeta(BaseModel):
    """Identity + capability advertisement for a connector."""

    name: str  # 'gsc', 'psi', 'csv', …
    version: str  # connector implementation version
    capabilities: list[str]  # e.g. ['cwv', 'lighthouse']


@runtime_checkable
class Connector(Protocol):
    """Marker protocol — connectors expose their capability methods directly."""

    meta: ConnectorMeta

    async def health_check(self) -> bool:
        """Quick auth + connectivity check (used by ``kvseo connect``)."""
        ...
