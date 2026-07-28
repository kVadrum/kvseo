"""Internal-link probe discipline (04-audit-engine.md §9).

The probe is the only audit stage that costs the audited host real traffic, so
the fences — cap, dedup, HEAD-then-GET retry, failures-are-not-exceptions — are
the behaviour worth pinning, not the happy path.
"""

from __future__ import annotations

import httpx
import pytest

from kvseo.core.audit.link_checker import MAX_LINKS, probe_links


def _client(handler: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_probes_return_status_codes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404 if request.url.path == "/gone" else 200)

    async with _client(handler) as client:
        result = await probe_links(
            ["https://example.com/ok", "https://example.com/gone"], client=client
        )

    assert result == {"https://example.com/ok": 200, "https://example.com/gone": 404}


@pytest.mark.asyncio
async def test_head_rejection_retries_as_get() -> None:
    """Plenty of hosts 405 on HEAD; that is a method problem, not a dead link."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(405) if request.method == "HEAD" else httpx.Response(200)

    async with _client(handler) as client:
        result = await probe_links(["https://example.com/head-hostile"], client=client)

    assert seen == ["HEAD", "GET"]
    assert result == {"https://example.com/head-hostile": 200}


@pytest.mark.asyncio
async def test_transport_failure_is_none_not_an_exception() -> None:
    """A dead host must degrade to an unreachable marker — one bad link cannot
    take down the audit."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with _client(handler) as client:
        result = await probe_links(["https://dead.test/x"], client=client)

    assert result == {"https://dead.test/x": None}


@pytest.mark.asyncio
async def test_caps_at_max_links() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200)

    urls = [f"https://example.com/p{i}" for i in range(MAX_LINKS + 25)]
    async with _client(handler) as client:
        result = await probe_links(urls, client=client)

    assert len(requested) == MAX_LINKS
    assert len(result) == MAX_LINKS


@pytest.mark.asyncio
async def test_duplicate_hrefs_are_probed_once() -> None:
    """A nav link repeated in header and footer is one destination — the cap
    should be spent on distinct URLs."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200)

    async with _client(handler) as client:
        result = await probe_links(
            ["https://example.com/a", "https://example.com/a", "https://example.com/b"],
            client=client,
        )

    assert len(requested) == 2
    assert result == {"https://example.com/a": 200, "https://example.com/b": 200}


@pytest.mark.asyncio
async def test_no_links_makes_no_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    async with _client(handler) as client:
        assert await probe_links([], client=client) == {}
