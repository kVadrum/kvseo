"""Fetcher transport behavior: streaming size cap, status/url passthrough,
charset decoding (04-audit-engine.md §7)."""

from __future__ import annotations

import httpx
import pytest

from kvseo.core.audit.fetcher import _MAX_BYTES, FetchError, fetch


def _client(response: httpx.Response) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: response))


async def test_fetch_returns_status_and_html() -> None:
    result = await fetch("https://example.com/", client=_client(httpx.Response(200, html="<title>ok</title>")))
    assert result.status_code == 200
    assert "<title>ok</title>" in result.html


async def test_fetch_rejects_oversize_body() -> None:
    # The cap aborts the read; the body is never buffered whole.
    big = httpx.Response(200, content=b"x" * (_MAX_BYTES + 1))
    with pytest.raises(FetchError) as exc:
        await fetch("https://example.com/", client=_client(big))
    assert exc.value.reason == "oversize"


async def test_fetch_decodes_declared_charset() -> None:
    body = "café".encode("latin-1")
    resp = httpx.Response(200, content=body, headers={"Content-Type": "text/html; charset=latin-1"})
    result = await fetch("https://example.com/", client=_client(resp))
    assert "café" in result.html
