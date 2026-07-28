"""Internal-link liveness probe (04-audit-engine.md §9).

This is the one audit input that costs the audited host real traffic, so the
spec fences it tightly: capped, concurrency-limited, short per-link timeout,
and off unless the user asks for it with ``--check-internal-links``.

It lives outside ``checks/`` on purpose. Checks are pure functions of a parsed
document and never touch the network (04 §2); the engine gathers this the same
way it gathers PSI and hands the result to ``internal_links.broken`` through
``AuditContext``. That keeps every check unit-testable without a transport.
"""

from __future__ import annotations

import asyncio

import httpx

from kvseo import __version__

MAX_LINKS = 50  # audit.internal_link_max
_CONCURRENCY = 8
_TIMEOUT = httpx.Timeout(5.0, connect=5.0)
_USER_AGENT = f"kvseo/{__version__} (+https://github.com/kvadrum/kvseo)"

# Status of a probed link: an HTTP status code, or None when the request never
# produced one (DNS failure, connection refused, timeout).
LinkStatus = dict[str, int | None]


async def probe_links(urls: list[str], *, client: httpx.AsyncClient | None = None) -> LinkStatus:
    """HEAD every URL (capped at ``MAX_LINKS``), returning url -> status code.

    Duplicate hrefs are collapsed before probing — a nav link repeated in a
    header and footer is one URL, and the cap should be spent on distinct
    destinations rather than on the same one twice.
    """
    unique = list(dict.fromkeys(urls))[:MAX_LINKS]
    if not unique:
        return {}

    active = client or httpx.AsyncClient(
        timeout=_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": _USER_AGENT},
    )
    semaphore = asyncio.Semaphore(_CONCURRENCY)
    try:
        statuses = await asyncio.gather(*(_probe(active, semaphore, u) for u in unique))
    finally:
        if client is None:
            await active.aclose()
    return dict(zip(unique, statuses, strict=True))


async def _probe(client: httpx.AsyncClient, semaphore: asyncio.Semaphore, url: str) -> int | None:
    async with semaphore:
        try:
            response = await client.head(url)
            # Plenty of hosts reject HEAD outright (405) or route it to a
            # handler that 501s; one GET retry distinguishes "method not
            # supported" from "link is actually broken".
            if response.status_code in (405, 501):
                response = await client.get(url)
            return response.status_code
        except httpx.HTTPError:
            return None
