"""GSC connector data path: parsing, persistence, freshness, error cascade.

Network mocked via httpx.MockTransport; a fake Bearer token stands in for the
OAuth-issued access token (the flow itself lives in gsc_auth).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorUnavailable,
)
from kvseo.connectors.gsc import GscConnector, GscPagePull, GscSite, property_for
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import GscQuery as GscQueryORM

START = date(2026, 3, 1)
END = date(2026, 5, 30)
SITE = "https://kemek.net/"
URL = "https://kemek.net/audit"

SITES_PAYLOAD = {"siteEntry": [{"siteUrl": SITE, "permissionLevel": "siteOwner"}]}
QUERIES_PAYLOAD = {
    "rows": [
        {"keys": ["seo audit"], "clicks": 12, "impressions": 300, "ctr": 0.04, "position": 7.5},
        {
            "keys": ["site audit tool"],
            "clicks": 3,
            "impressions": 120,
            "ctr": 0.025,
            "position": 14.2,
        },
    ]
}


def _client(
    payload: Any, *, status: int = 200, headers: Mapping[str, str] | None = None
) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=dict(headers or {}))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_conforms_to_connector_protocol() -> None:
    assert isinstance(GscConnector(access_token="t"), Connector)


async def test_sites_parses() -> None:
    conn = GscConnector(access_token="tok", client=_client(SITES_PAYLOAD))
    assert await conn.sites() == [
        GscSite(site_url=SITE, permission_level="siteOwner")
    ]


async def test_queries_for_url_parses() -> None:
    conn = GscConnector(access_token="tok", client=_client(QUERIES_PAYLOAD))
    rows = await conn.queries_for_url(SITE, URL, START, END)
    assert [r.query for r in rows] == ["seo audit", "site audit tool"]
    assert rows[0].page == URL  # page comes from the filter, not the row keys
    assert rows[0].clicks == 12
    assert rows[0].date_range_start == START


async def test_queries_for_site_uses_page_key() -> None:
    payload = {
        "rows": [
            {
                "keys": ["seo", "https://kemek.net/p1"],
                "clicks": 1,
                "impressions": 10,
                "ctr": 0.1,
                "position": 3.0,
            }
        ]
    }
    conn = GscConnector(access_token="tok", client=_client(payload))
    rows = await conn.queries_for_site(SITE, START, END)
    assert rows[0].query == "seo"
    assert rows[0].page == "https://kemek.net/p1"  # second dimension key


async def test_persist_then_freshness_serves_cache(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    conn = GscConnector(access_token="tok", engine=engine, client=_client(QUERIES_PAYLOAD))
    await conn.queries_for_url(SITE, URL, START, END)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(GscQueryORM)) == 2
    # A second connector whose client would 500 must still succeed from cache.
    cached = await GscConnector(
        access_token="tok", engine=engine, client=_client({}, status=500)
    ).queries_for_url(SITE, URL, START, END)
    assert {r.query for r in cached} == {"seo audit", "site audit tool"}
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(GscQueryORM)) == 2  # no refetch


async def test_refresh_bypasses_cache(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    conn = GscConnector(access_token="tok", engine=engine, client=_client(QUERIES_PAYLOAD))
    await conn.queries_for_url(SITE, URL, START, END)
    await GscConnector(
        access_token="tok", engine=engine, client=_client(QUERIES_PAYLOAD)
    ).queries_for_url(SITE, URL, START, END, refresh=True)
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(GscQueryORM)) == 4


async def test_different_date_range_misses_cache(tmp_path: Path) -> None:
    # Same site/URL but a different period must NOT serve the prior range's rows
    # from cache (even within the freshness window) — it must fetch the requested
    # period afresh, or callers get metrics mislabeled with the wrong dates.
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    await GscConnector(
        access_token="tok", engine=engine, client=_client(QUERIES_PAYLOAD)
    ).queries_for_url(SITE, URL, START, END)

    other_start, other_end = date(2026, 1, 1), date(2026, 1, 31)
    rows = await GscConnector(
        access_token="tok", engine=engine, client=_client(QUERIES_PAYLOAD)
    ).queries_for_url(SITE, URL, other_start, other_end)

    assert rows[0].date_range_start == other_start  # the requested period, fetched fresh
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(GscQueryORM)) == 4  # refetched, not cache-served


async def test_not_connected_raises_auth_error() -> None:
    conn = GscConnector(access_token=None, client=_client(SITES_PAYLOAD))
    with pytest.raises(ConnectorAuthError):
        await conn.sites()


async def test_auth_error_on_403() -> None:
    conn = GscConnector(access_token="tok", client=_client({}, status=403))
    with pytest.raises(ConnectorAuthError):
        await conn.sites()


async def test_rate_limited_reads_retry_after() -> None:
    conn = GscConnector(
        access_token="tok", client=_client({}, status=429, headers={"Retry-After": "45"})
    )
    with pytest.raises(ConnectorRateLimited) as exc:
        await conn.sites()
    assert exc.value.retry_after == 45


async def test_server_error_is_unavailable() -> None:
    conn = GscConnector(access_token="tok", client=_client({}, status=503))
    with pytest.raises(ConnectorUnavailable):
        await conn.sites()


# --- the audit's page pull -------------------------------------------------


@pytest.mark.parametrize(
    ("sites", "url", "expected"),
    [
        # The longest covering URL-prefix wins over a broader one and a domain.
        (
            [("sc-domain:kemek.net", "siteOwner"), ("https://kemek.net/", "siteOwner"),
             ("https://kemek.net/blog/", "siteFullUser")],
            "https://kemek.net/blog/post",
            "https://kemek.net/blog/",
        ),
        # A URL-prefix property is scheme-exact; the domain property covers both.
        ([("http://kemek.net/", "siteOwner"), ("sc-domain:kemek.net", "siteOwner")],
         "https://kemek.net/x", "sc-domain:kemek.net"),
        # Domain properties cover subdomains, but not lookalike hosts.
        ([("sc-domain:kemek.net", "siteOwner")], "https://www.kemek.net/", "sc-domain:kemek.net"),
        ([("sc-domain:kemek.net", "siteOwner")], "https://notkemek.net/", None),
        # An unverified grant cannot be queried, so it never matches.
        ([("https://kemek.net/", "siteUnverifiedUser")], "https://kemek.net/a", None),
        ([], "https://kemek.net/a", None),
    ],
)
def test_property_for(sites: list[tuple[str, str]], url: str, expected: str | None) -> None:
    gsc_sites = [GscSite(site_url=s, permission_level=p) for s, p in sites]
    assert property_for(url, gsc_sites) == expected


def _routed(seen: list[httpx.Request], sites: Any) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/sites"):
            return httpx.Response(200, json=sites)
        return httpx.Response(200, json=QUERIES_PAYLOAD)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_pull_page_queries_the_covering_property_over_90_days(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    seen: list[httpx.Request] = []
    conn = GscConnector(access_token="tok", engine=engine, client=_routed(seen, SITES_PAYLOAD))

    pull = await conn.pull_page(URL, today=date(2026, 6, 10))

    assert pull == GscPagePull(property=SITE, queries=2)
    body = json.loads(seen[1].content)
    # Ends three days back (GSC's finalisation lag) and spans 90 days inclusive.
    assert (body["startDate"], body["endDate"]) == ("2026-03-10", "2026-06-07")
    assert body["dimensionFilterGroups"][0]["filters"][0]["expression"] == URL
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(GscQueryORM)) == 2


async def test_pull_page_outside_every_property_is_a_result_not_an_error() -> None:
    seen: list[httpx.Request] = []
    conn = GscConnector(access_token="tok", client=_routed(seen, SITES_PAYLOAD))
    assert await conn.pull_page("https://elsewhere.example/") == GscPagePull(property=None, queries=0)
    assert len(seen) == 1  # listed the properties, queried nothing
