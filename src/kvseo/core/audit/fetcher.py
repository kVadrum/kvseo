"""URL fetcher for the audit engine (04-audit-engine.md §7).

A thin async httpx wrapper: 30s total / 10s connect timeout, ≤5 redirects, a
kvseo User-Agent, a 5 MB size cap. No JavaScript execution — SPA-heavy pages
get a partial DOM (a known v0.1 limitation; headless Chromium is v0.3+).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from kvseo import __version__

_USER_AGENT = f"kvseo/{__version__} (+https://github.com/kvadrum/kvseo)"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_BYTES = 5 * 1024 * 1024


class FetchError(Exception):
    """Fetch failed; ``reason`` becomes ``audit_run.failure_reason``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str  # after redirects
    status_code: int
    html: str
    duration_ms: int


async def fetch(url: str, *, client: httpx.AsyncClient | None = None) -> FetchResult:
    owns_client = client is None
    active = client or httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        max_redirects=5,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    start = time.monotonic()
    try:
        resp = await active.get(url)
    except httpx.TimeoutException as exc:
        raise FetchError("timeout") from exc
    except httpx.TooManyRedirects as exc:
        raise FetchError("too_many_redirects") from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"network: {exc}") from exc
    finally:
        if owns_client:
            await active.aclose()
    duration_ms = int((time.monotonic() - start) * 1000)
    if len(resp.content) > _MAX_BYTES:
        raise FetchError("oversize")
    return FetchResult(
        requested_url=url,
        final_url=str(resp.url),
        status_code=resp.status_code,
        html=resp.text,
        duration_ms=duration_ms,
    )
