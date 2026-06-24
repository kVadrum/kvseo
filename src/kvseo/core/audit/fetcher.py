"""URL fetcher for the audit engine (04-audit-engine.md §7).

A thin async httpx wrapper: 30s total / 10s connect timeout, ≤5 redirects, a
kvseo User-Agent. The body is streamed and the read aborts the moment it
crosses a 5 MB cap, so a hostile or misconfigured host can't buffer a
multi-GB / unbounded response whole into memory (a real concern under
Poseidon's memory-capped user slice). No JavaScript execution — SPA-heavy
pages get a partial DOM (a known v0.1 limitation; headless Chromium is v0.3+).

Redirects are followed without host re-validation. kvseo is a local
single-operator CLI auditing its own / clients' sites, so the SSRF-shaped
"redirect to 169.254.169.254 / localhost" vector crosses no privilege
boundary the operator doesn't already have; httpx also rejects non-http
schemes (``file://``) as ``UnsupportedProtocol``. This becomes a real concern
only if kvseo is ever exposed as a hosted/multi-tenant service — revisit then.
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
        async with active.stream("GET", url) as resp:
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > _MAX_BYTES:
                    # Abort mid-stream — never finish buffering an oversize body.
                    raise FetchError("oversize")
            # charset_encoding / status / url are header-derived, so they're
            # available without httpx having read the (streamed) body.
            encoding = resp.charset_encoding or "utf-8"
            status_code = resp.status_code
            final_url = str(resp.url)
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
    # Honor the declared charset; fall back to utf-8 (replacing undecodable
    # bytes) rather than raise — HTML is overwhelmingly utf-8 and a partial
    # decode beats failing the whole audit on one stray byte.
    try:
        html = bytes(body).decode(encoding)
    except (LookupError, UnicodeDecodeError):
        html = bytes(body).decode("utf-8", errors="replace")
    return FetchResult(
        requested_url=url,
        final_url=final_url,
        status_code=status_code,
        html=html,
        duration_ms=duration_ms,
    )
