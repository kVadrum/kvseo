"""SerpBear connector: the /api/keywords shape, SerpBear's own conventions
(position 0 = unranked, unpadded history dates), and which stored rankings bear
on an audited page. Network mocked via httpx.MockTransport."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from kvseo.connectors.base import Connector, ConnectorAuthError, ConnectorUnavailable
from kvseo.connectors.serpbear import SerpBearConnector, rankings_for_page
from kvseo.storage.db import get_engine, migrate

KEYWORDS = {
    "keywords": [
        {
            "ID": 1,
            "keyword": "seo audit",
            "device": "desktop",
            "country": "US",
            "domain": "kemek.net",
            "lastUpdated": "2026-11-15T06:00:00.000Z",
            "position": 14,
            # SerpBear's real key format: unpadded, so "2026-11-9" sorts after "2026-11-15" as text.
            "history": {"2026-11-15": 14, "2026-11-9": 19, "2026-10-30": 0},
            "url": "https://www.kemek.net/audit/",
        },
        {
            "ID": 2,
            "keyword": "ops consulting",
            "device": "desktop",
            "country": "US",
            "domain": "kemek.net",
            "lastUpdated": "2026-11-15T06:00:00.000Z",
            "position": 0,
            "history": {},
            "url": "",
        },
        {
            "ID": 3,
            "keyword": "kemek network",
            "device": "desktop",
            "country": "US",
            "domain": "kemek.net",
            "lastUpdated": "2026-11-15T06:00:00.000Z",
            "position": 1,
            "history": {},
            "url": "https://kemek.net/",
        },
    ]
}


def _client(payload: Any, seen: list[httpx.Request] | None = None, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_conforms_to_connector_protocol() -> None:
    assert isinstance(SerpBearConnector("https://sb.example", "k"), Connector)


async def test_keywords_parse_serpbear_conventions() -> None:
    seen: list[httpx.Request] = []
    conn = SerpBearConnector("https://sb.example/", "key", client=_client(KEYWORDS, seen))
    rankings = await conn.keywords("kemek.net")

    assert str(seen[0].url) == "https://sb.example/api/keywords?domain=kemek.net"
    assert seen[0].headers["authorization"] == "Bearer key"
    audit = rankings[0]
    assert audit.position == 14
    assert [(p.date, p.position) for p in audit.history] == [
        ("2026-10-30", None),  # 0 = not in what SerpBear scraped
        ("2026-11-09", 19),
        ("2026-11-15", 14),
    ]
    unranked = rankings[1]
    assert (unranked.position, unranked.url) == (None, None)


async def test_auth_and_non_serpbear_responses() -> None:
    with pytest.raises(ConnectorAuthError):
        await SerpBearConnector("https://sb.example", "bad", client=_client({}, status=401)).domains()

    def html(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not serpbear</html>")

    conn = SerpBearConnector("https://sb.example", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(html)))
    with pytest.raises(ConnectorUnavailable, match=r"is https://sb\.example a SerpBear"):
        await conn.domains()


async def test_rankings_for_page_match_the_page_or_the_target_keyword(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    await SerpBearConnector("https://sb.example", "k", engine=engine, client=_client(KEYWORDS)).keywords("kemek.net")

    with Session(engine) as session:
        # The tracked URL differs by www and a trailing slash; it is the same page.
        by_page = rankings_for_page(session, "https://kemek.net/audit", None, limit=25)
        assert [r.keyword for r in by_page] == ["seo audit"]
        # The target keyword counts wherever it ranks, even unranked.
        with_target = rankings_for_page(session, "https://kemek.net/audit", "ops consulting", limit=25)
        assert sorted(r.keyword for r in with_target) == ["ops consulting", "seo audit"]
        assert rankings_for_page(session, "https://elsewhere.example/", None, limit=25) == []
