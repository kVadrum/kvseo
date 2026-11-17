"""OpenSEO connector: the MCP Streamable HTTP handshake (session id, JSON and
event-stream replies), tool-result mapping, and project selection. A fake MCP
server stands in via httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from kvseo.connectors.base import Connector, ConnectorAuthError, ConnectorUnavailable
from kvseo.connectors.openseo import OpenSeoConnector, _response_message, latest_keyword_metrics
from kvseo.storage.db import get_engine, migrate

PROJECTS = {"projects": [{"id": "p1", "name": "Agency", "domain": "agency.example"},
                         {"id": "p2", "name": "KeMeK", "domain": "www.kemek.net"}]}
METRICS = {
    "keywords": [
        {
            "keyword": "seo audit",
            "search_volume": 2900,
            "keyword_difficulty": 41,
            "main_intent": "commercial",
            "cpc": 6.1,
            "competition": 0.42,
            "competition_level": "MEDIUM",
            "monthly_searches": None,
        }
    ]
}


class FakeMcp:
    """Records every JSON-RPC message; answers tools/call from ``tools``."""

    def __init__(self, tools: dict[str, Any], *, sse: bool = True, status: int = 200) -> None:
        self.tools = tools
        self.sse = sse
        self.status = status
        self.messages: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status)
        message = json.loads(request.content)
        self.messages.append(message)
        self.headers.append(request.headers)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            reply = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
            return self._reply(message["id"], reply, {"mcp-session-id": "sess-1"})
        name = message["params"]["name"]
        tool = self.tools[name]
        result = tool if "isError" in tool else {"content": [{"type": "text", "text": "ok"}], "structuredContent": tool}
        return self._reply(message["id"], result)

    def _reply(self, msg_id: int, result: dict[str, Any], headers: dict[str, str] | None = None) -> httpx.Response:
        body = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if not self.sse:
            return httpx.Response(200, json=body, headers=headers)
        stream = f"event: message\ndata: {json.dumps(body)}\n\n"
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream", **(headers or {})})


async def test_conforms_to_connector_protocol() -> None:
    assert isinstance(OpenSeoConnector("oseo_k"), Connector)


@pytest.mark.parametrize("sse", [True, False])
async def test_handshake_then_tool_call_carries_session_and_key(sse: bool) -> None:
    fake = FakeMcp({"list_projects": PROJECTS}, sse=sse)
    conn = OpenSeoConnector("oseo_key", url="https://seo.example", client=fake.client())

    projects = await conn.projects()

    assert [p.id for p in projects] == ["p1", "p2"]
    assert [m["method"] for m in fake.messages] == ["initialize", "notifications/initialized", "tools/call"]
    assert all(h["authorization"] == "Bearer oseo_key" for h in fake.headers)
    assert "mcp-session-id" not in fake.headers[0]
    assert fake.headers[2]["mcp-session-id"] == "sess-1"


async def test_keyword_metrics_map_and_cache(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    fake = FakeMcp({"get_keyword_metrics": METRICS})
    rows = await OpenSeoConnector("k", engine=engine, client=fake.client()).keyword_volume(
        ["SEO audit"], project_id="p2"
    )
    assert rows[0].model_dump() == {
        "keyword": "seo audit",
        "monthly_volume": 2900,
        "cpc_usd": 6.1,
        "competition": 0.42,
        "keyword_difficulty": 41,
        "intent": "commercial",
    }
    call = fake.messages[-1]["params"]
    assert call == {
        "name": "get_keyword_metrics",
        "arguments": {"projectId": "p2", "keywords": ["seo audit"], "includeMonthlyTrends": False},
    }

    again = FakeMcp({})
    cached = await OpenSeoConnector("k", engine=engine, client=again.client()).keyword_volume(
        ["seo audit"], project_id="p2"
    )
    assert cached == rows
    assert again.messages == []  # no credits spent on a fresh cache hit


async def test_project_for_prefers_the_audited_domain() -> None:
    conn = OpenSeoConnector("k", client=FakeMcp({"list_projects": PROJECTS}).client())
    assert (await conn.project_for("https://kemek.net/audit")).id == "p2"
    conn = OpenSeoConnector("k", client=FakeMcp({"list_projects": PROJECTS}).client())
    assert (await conn.project_for("elsewhere.example")).id == "p1"  # first project: market only
    conn = OpenSeoConnector("k", client=FakeMcp({"list_projects": {"projects": []}}).client())
    with pytest.raises(ConnectorUnavailable, match="no projects"):
        await conn.project_for("kemek.net")


async def test_tool_errors_and_rejected_keys() -> None:
    failing = {"get_domain_overview": {"isError": True, "content": [{"type": "text", "text": "Out of credits"}]}}
    with pytest.raises(ConnectorUnavailable, match="Out of credits"):
        await OpenSeoConnector("k", client=FakeMcp(failing).client()).domain_overview("kemek.net", project_id="p2")
    with pytest.raises(ConnectorAuthError):
        await OpenSeoConnector("bad", client=FakeMcp({}, status=401).client()).projects()


async def test_serp_reads_best_position_and_reports_failed_keywords() -> None:
    serp = {
        "results": [
            {
                "keyword": "seo audit",
                "ok": True,
                "items": [
                    {"type": "organic", "rank": 1, "bestPosition": 2, "seenIn": 3, "title": "A",
                     "url": "https://a.example/", "domain": "a.example", "description": None},
                    {"type": "people_also_ask", "rank": None, "bestPosition": None, "seenIn": 1, "title": None,
                     "url": None, "domain": None, "description": None},
                ],
            }
        ]
    }
    result = await OpenSeoConnector("k", client=FakeMcp({"get_serp_results": serp}).client()).serp(
        "seo audit", project_id="p2"
    )
    assert [(r.position, r.domain) for r in result.results] == [(2, "a.example")]

    failed = {"results": [{"keyword": "seo audit", "ok": False, "error": "timed out"}]}
    with pytest.raises(ConnectorUnavailable, match="timed out"):
        await OpenSeoConnector("k", client=FakeMcp({"get_serp_results": failed}).client()).serp(
            "seo audit", project_id="p2"
        )


async def test_a_project_without_a_domain_key_still_parses() -> None:
    projects = {"projects": [{"id": "p1", "name": "Scratch"}]}
    conn = OpenSeoConnector("k", client=FakeMcp({"list_projects": projects}).client())
    assert (await conn.project_for("kemek.net")).id == "p1"


async def test_the_keyword_cache_is_per_project(tmp_path: Path) -> None:
    """A project sets the market, so p2's numbers are no answer for p1."""
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    first = FakeMcp({"get_keyword_metrics": METRICS})
    await OpenSeoConnector("k", engine=engine, client=first.client()).keyword_volume(["seo audit"], project_id="p2")
    other = FakeMcp({"get_keyword_metrics": METRICS})
    await OpenSeoConnector("k", engine=engine, client=other.client()).keyword_volume(["seo audit"], project_id="p1")
    assert [m.get("method") for m in other.messages][-1] == "tools/call"
    with Session(engine) as session:
        stored = latest_keyword_metrics(session, "SEO audit")
        assert stored is not None and stored[0].monthly_volume == 2900


def test_event_stream_joins_multiline_data_and_skips_server_requests() -> None:
    """SSE joins an event's data lines; a server request reusing the id is not the reply."""
    reply = {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
    data = "\n".join(f"data: {line}" for line in json.dumps(reply, indent=1).splitlines())
    stream = f'event: message\ndata: {{"jsonrpc": "2.0", "id": 2, "method": "ping"}}\n\nevent: message\n{data}\n\n'
    resp = httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})
    assert _response_message(resp, 2) == reply
