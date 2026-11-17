"""DataForSEO connector: response flattening, billed-cost persistence, the
week-long keyword cache, and the in-body status codes DataForSEO uses instead
of HTTP errors. Network mocked via httpx.MockTransport."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.connectors.base import Connector, ConnectorAuthError, ConnectorRateLimited, ConnectorUnavailable
from kvseo.connectors.dataforseo import (
    SANDBOX_URL,
    DataForSeoConnector,
    DfsKeywordVolume,
    latest_keyword_volume,
    persist_keyword_volumes,
)
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import DataForSeoData


def _envelope(result: list[dict[str, Any]], *, cost: float = 0.075, task_status: int = 20000) -> dict[str, Any]:
    return {
        "status_code": 20000,
        "status_message": "Ok.",
        "cost": cost,
        "tasks": [
            {"status_code": task_status, "status_message": "Ok." if task_status == 20000 else "x", "result": result}
        ],
    }


VOLUME = _envelope(
    [
        {
            "keyword": "seo audit",
            "location_code": 2840,
            "language_code": "en",
            "search_volume": 2900,
            "cpc": 6.12,
            "competition": "MEDIUM",
            "competition_index": 42,
            "monthly_searches": [{"year": 2026, "month": 9, "search_volume": 2400}],
        },
        {"keyword": "site audit tool", "search_volume": None, "cpc": None, "competition_index": None},
    ],
    cost=0.075,
)


def _client(payload: Any, seen: list[httpx.Request] | None = None, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    db = tmp_path / "kvseo.db"
    migrate(db)
    return get_engine(db)


async def test_conforms_to_connector_protocol() -> None:
    assert isinstance(DataForSeoConnector("l", "p"), Connector)


async def test_keyword_volume_flattens_and_bills_each_row(engine: Any) -> None:
    seen: list[httpx.Request] = []
    conn = DataForSeoConnector("login", "pw", engine=engine, client=_client(VOLUME, seen))

    rows = await conn.keyword_volume(["SEO audit ", "site audit tool"])

    assert [r.keyword for r in rows] == ["seo audit", "site audit tool"]
    assert rows[0].search_volume == 2900
    assert rows[0].competition == pytest.approx(0.42)  # competition_index / 100
    assert rows[0].monthly_searches[0].search_volume == 2400
    assert rows[1].competition is None
    request = seen[0]
    assert request.url.path == "/v3/keywords_data/google_ads/search_volume/live"
    assert request.headers["authorization"].startswith("Basic ")
    assert json.loads(request.content) == [
        {"keywords": ["seo audit", "site audit tool"], "location_code": 2840, "language_code": "en"}
    ]
    with Session(engine) as session:
        costs = session.scalars(select(DataForSeoData.estimated_cost_usd)).all()
        assert sum(c or 0 for c in costs) == pytest.approx(0.075)  # the call's cost, split, sums back
        stored = latest_keyword_volume(session, "seo audit")
        assert stored is not None and stored[1] == "api"


async def test_cached_keywords_are_not_billed_again(engine: Any) -> None:
    await DataForSeoConnector("l", "p", engine=engine, client=_client(VOLUME)).keyword_volume(["seo audit"])
    seen: list[httpx.Request] = []
    again = DataForSeoConnector("l", "p", engine=engine, client=_client(VOLUME, seen))
    rows = await again.keyword_volume(["seo audit"])
    assert [r.search_volume for r in rows] == [2900]
    assert seen == []  # served from dataforseo_data, nothing billed
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(DataForSeoData)) == 2


@pytest.mark.parametrize("other_source", ["csv", "sandbox"])
async def test_the_cache_serves_only_rows_this_endpoint_fetched(engine: Any, other_source: str) -> None:
    """A CSV import or sandbox sample in the same table is not a live call, so
    a live connector still asks DataForSEO."""
    stale = DfsKeywordVolume(keyword="seo audit", location_code=2840, language_code="en", search_volume=12345,
                             cpc_usd=None, competition=None, monthly_searches=[], fetched_at=datetime.now(UTC))
    persist_keyword_volumes(engine, [stale], cost=None, source=other_source)
    seen: list[httpx.Request] = []
    rows = await DataForSeoConnector("l", "p", engine=engine, client=_client(VOLUME, seen)).keyword_volume(
        ["seo audit"]
    )
    assert [r.search_volume for r in rows] == [2900]
    assert len(seen) == 1


async def test_sandbox_rows_are_tagged_and_never_inform_advice(engine: Any) -> None:
    """The sandbox answers with sample numbers: stored as 'sandbox', served back
    only to a sandbox connection, and never read by the advisor."""
    sandbox = DataForSeoConnector("l", "p", sandbox=True, engine=engine, client=_client(VOLUME))
    await sandbox.keyword_volume(["seo audit"])
    with Session(engine) as session:
        assert set(session.scalars(select(DataForSeoData.source)).all()) == {"sandbox"}
        assert latest_keyword_volume(session, "seo audit") is None
    seen: list[httpx.Request] = []
    await DataForSeoConnector("l", "p", sandbox=True, engine=engine, client=_client(VOLUME, seen)).keyword_volume(
        ["seo audit"]
    )
    assert seen == []  # the sandbox's own cache still works


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        # DataForSEO reports failures in the body of an HTTP 200.
        ({"status_code": 40100, "status_message": "You are not authorized.", "tasks": []}, ConnectorAuthError),
        ({"status_code": 40200, "status_message": "Insufficient funds.", "tasks": []}, ConnectorAuthError),
        ({"status_code": 40210, "status_message": "Balance too low.", "tasks": []}, ConnectorAuthError),
        # Neighbours in the 401xx/402xx band that are not the account's fault.
        ({"status_code": 40202, "status_message": "Rate limit per minute.", "tasks": []}, ConnectorRateLimited),
        (_envelope([], task_status=40102), ConnectorUnavailable),  # no search results
        ({"status_code": 50000, "status_message": "Internal error.", "tasks": []}, ConnectorUnavailable),
        (_envelope([], task_status=40501), ConnectorUnavailable),  # a per-task failure
    ],
)
async def test_body_status_codes_map_to_connector_errors(payload: dict[str, Any], error: type[Exception]) -> None:
    conn = DataForSeoConnector("l", "p", client=_client(payload))
    with pytest.raises(error):
        await conn.keyword_volume(["seo audit"])


async def test_http_401_is_an_auth_error() -> None:
    with pytest.raises(ConnectorAuthError):
        await DataForSeoConnector("l", "p", client=_client({}, status=401)).health_check()


async def test_serp_keeps_organic_rows_only() -> None:
    payload = _envelope(
        [
            {
                "items": [
                    {"type": "featured_snippet", "rank_group": 1, "domain": "a.example"},
                    {"type": "organic", "rank_group": 1, "domain": "b.example", "url": "https://b.example/"},
                    {"type": "organic", "rank_group": 2, "domain": "c.example", "url": "https://c.example/"},
                ]
            }
        ]
    )
    serp = await DataForSeoConnector("l", "p", client=_client(payload)).serp("seo audit")
    assert [(r.rank, r.domain) for r in serp.results] == [(1, "b.example"), (2, "c.example")]


async def test_keyword_ideas_flatten_labs_items() -> None:
    payload = _envelope(
        [
            {
                "items": [
                    {
                        "keyword": "free seo audit",
                        "keyword_info": {"search_volume": 1300, "cpc": 4.1, "competition": 0.3},
                        "keyword_properties": {"keyword_difficulty": 38},
                        "search_intent_info": {"main_intent": "commercial"},
                    }
                ]
            }
        ]
    )
    ideas = await DataForSeoConnector("l", "p", client=_client(payload)).keyword_ideas("seo audit")
    assert ideas[0].model_dump() == {
        "keyword": "free seo audit",
        "search_volume": 1300,
        "cpc_usd": 4.1,
        "competition": 0.3,
        "keyword_difficulty": 38,
        "intent": "commercial",
    }


async def test_sandbox_targets_the_sandbox_host() -> None:
    seen: list[httpx.Request] = []
    await DataForSeoConnector("l", "p", sandbox=True, client=_client({"status_code": 20000}, seen)).health_check()
    assert str(seen[0].url).startswith(SANDBOX_URL)
