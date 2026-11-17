"""OpenSEO connector — keyword metrics, SERPs, domain overview (03-connector-interfaces.md §4).

OpenSEO has no REST API; its integration surface is an MCP server (``/mcp``,
hosted at app.openseo.so or on a self-hosted instance), and its data comes from
DataForSEO underneath. 03 §4 sketched plain HTTP with a bearer token; this
speaks the MCP Streamable HTTP transport instead — JSON-RPC over POST, with the
reply as JSON or as a server-sent event — authenticated by an OpenSEO API key
(``oseo_…``) as a Bearer token. Every OpenSEO research tool is scoped to a
project, so calls carry the project whose domain matches the audited site, or
the account's first project (which only sets the default market).

Rows persist to ``openseo_data`` (07 §3.7); keyword metrics are served from
there for a week, since OpenSEO bills credits per call.
"""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import ColumnElement, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo import __version__
from kvseo.connectors.base import ConnectorAuthError, ConnectorMeta, ConnectorUnavailable
from kvseo.storage.models import OpenSeoData
from kvseo.storage.timestamps import now_str, parse_ts

HOSTED_URL = "https://app.openseo.so"
_PROTOCOL_VERSION = "2025-06-18"
_TIMEOUT = httpx.Timeout(90.0)  # SERP reads take 10-30s by OpenSEO's own account
_KEYWORD_FRESHNESS = timedelta(days=7)


class OpenSeoProject(BaseModel):
    id: str
    name: str
    domain: str | None = None  # absent, not just null, on a project with no site set


class OpenSeoKeyword(BaseModel):
    keyword: str
    monthly_volume: int | None
    cpc_usd: float | None
    competition: float | None  # 0.0-1.0
    keyword_difficulty: int | None  # 0-100
    intent: str | None  # 'informational', 'commercial', ...


class OpenSeoSerpResult(BaseModel):
    position: int | None  # the best organic position any of OpenSEO's reads gave
    domain: str | None
    url: str | None
    title: str | None


class OpenSeoSerp(BaseModel):
    keyword: str
    fetched_at: datetime
    results: list[OpenSeoSerpResult]


class OpenSeoDomainOverview(BaseModel):
    domain: str
    organic_traffic: float | None
    organic_keywords: int | None


class OpenSeoConnector:
    """Read-only OpenSEO client over its MCP server."""

    meta = ConnectorMeta(
        name="openseo", version="0.1.0", capabilities=["keyword_volume", "serp", "domain_overview"]
    )

    def __init__(
        self,
        token: str,
        *,
        url: str = HOSTED_URL,
        engine: Engine | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base = url.rstrip("/")
        self._endpoint = base if base.endswith("/mcp") else base + "/mcp"
        self._token = token
        self._engine = engine
        self._client = client
        self._session_id: str | None = None
        self._initialized = False
        self._ids = itertools.count(1)

    async def health_check(self) -> bool:
        await self.projects()
        return True

    async def projects(self) -> list[OpenSeoProject]:
        data = await self._call_tool("list_projects", {})
        return [OpenSeoProject.model_validate(p) for p in data.get("projects") or []]

    async def project_for(self, domain: str) -> OpenSeoProject:
        """The project for ``domain``, else the first one (it only sets the market)."""
        projects = await self.projects()
        if not projects:
            raise ConnectorUnavailable("OpenSEO has no projects yet — create one in OpenSEO first")
        host = _bare(domain)
        return next((p for p in projects if p.domain and _bare(p.domain) == host), projects[0])

    async def keyword_volume(
        self, keywords: list[str], *, project_id: str, refresh: bool = False
    ) -> list[OpenSeoKeyword]:
        """Volume, CPC, competition, difficulty and intent per keyword (``get_keyword_metrics``)."""
        wanted = list(dict.fromkeys(k.strip().lower() for k in keywords if k.strip()))
        cached = {} if refresh else self._cached(wanted, project_id)
        missing = [k for k in wanted if k not in cached]
        if missing:
            data = await self._call_tool(
                "get_keyword_metrics",
                {"projectId": project_id, "keywords": missing, "includeMonthlyTrends": False},
            )
            fresh = [_keyword(row) for row in data.get("keywords") or []]
            self._persist(
                "keyword_volume", [(_metrics_key(project_id, k.keyword), k.model_dump()) for k in fresh]
            )
            cached.update({k.keyword.lower(): k for k in fresh})
        return [cached[k] for k in wanted if k in cached]

    async def serp(self, keyword: str, *, project_id: str) -> OpenSeoSerp:
        """Google organic results for ``keyword`` (``get_serp_results``, page 1)."""
        data = await self._call_tool("get_serp_results", {"projectId": project_id, "queries": [{"keyword": keyword}]})
        result = next(iter(data.get("results") or []), None)
        if result is None or not result.get("ok"):
            reason = (result or {}).get("error", "no result")
            raise ConnectorUnavailable(f"OpenSEO couldn't read the SERP for '{keyword}': {reason}")
        serp = OpenSeoSerp(
            keyword=keyword,
            fetched_at=datetime.now(UTC),
            results=[
                OpenSeoSerpResult(
                    position=item.get("bestPosition"),
                    domain=item.get("domain"),
                    url=item.get("url"),
                    title=item.get("title"),
                )
                for item in result.get("items") or []
                if item.get("type") in (None, "organic")
            ],
        )
        self._persist("serp", [(_metrics_key(project_id, keyword), serp.model_dump(mode="json"))])
        return serp

    async def domain_overview(self, domain: str, *, project_id: str) -> OpenSeoDomainOverview:
        """Estimated organic traffic and ranked-keyword count (``get_domain_overview``)."""
        data = await self._call_tool("get_domain_overview", {"projectId": project_id, "domain": domain})
        overview = OpenSeoDomainOverview(
            domain=str(data.get("domain") or domain),
            organic_traffic=data.get("organicTraffic"),
            organic_keywords=data.get("organicKeywords"),
        )
        self._persist("domain", [(_bare(domain), overview.model_dump())])
        return overview

    # --- MCP over Streamable HTTP -----------------------------------------

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            if not self._initialized:
                await self._rpc(client, "initialize", {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "kvseo", "version": __version__},
                })
                await self._notify(client, "notifications/initialized")
                self._initialized = True
            result = await self._rpc(client, "tools/call", {"name": name, "arguments": arguments})
        finally:
            if self._client is None:
                await client.aclose()
                self._initialized = False  # a session lives as long as its client
                self._session_id = None
        if result.get("isError"):
            text = " ".join(c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text")
            raise ConnectorUnavailable(f"OpenSEO {name} failed: {text.strip() or 'no detail'}")
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            raise ConnectorUnavailable(f"OpenSEO {name} returned no structured result")
        return structured

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
        msg_id = next(self._ids)
        resp = await self._post(client, {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        if session := resp.headers.get("mcp-session-id"):
            self._session_id = session
        message = _response_message(resp, msg_id)
        if "error" in message:
            error = message["error"]
            raise ConnectorUnavailable(f"OpenSEO {method} error {error.get('code')}: {error.get('message')}")
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def _notify(self, client: httpx.AsyncClient, method: str) -> None:
        await self._post(client, {"jsonrpc": "2.0", "method": method})

    async def _post(self, client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": _PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = await client.post(self._endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(
                f"couldn't reach OpenSEO at {self._endpoint} ({exc}) — check the URL given to `kvseo connect openseo`"
            ) from exc
        if resp.status_code in (401, 403):
            raise ConnectorAuthError("OpenSEO rejected the API key — re-run `kvseo connect openseo`")
        if resp.status_code >= 400:
            raise ConnectorUnavailable(f"OpenSEO returned {resp.status_code}")
        return resp

    # --- Persistence + freshness -----------------------------------------

    def _persist(self, resource_type: str, rows: list[tuple[str, Any]]) -> None:
        if self._engine is None or not rows:
            return
        fetched_at = now_str()
        with Session(self._engine) as session:
            session.add_all(
                OpenSeoData(resource_type=resource_type, resource_key=key, payload=payload, fetched_at=fetched_at)
                for key, payload in rows
            )
            session.commit()

    def _cached(self, keywords: list[str], project_id: str) -> dict[str, OpenSeoKeyword]:
        if self._engine is None:
            return {}
        found: dict[str, OpenSeoKeyword] = {}
        with Session(self._engine) as session:
            for keyword in keywords:
                row = _latest(session, OpenSeoData.resource_key == _metrics_key(project_id, keyword))
                if row is not None and datetime.now(UTC) - parse_ts(row.fetched_at) <= _KEYWORD_FRESHNESS:
                    found[keyword] = OpenSeoKeyword.model_validate(row.payload)
        return found


def latest_keyword_metrics(session: Session, keyword: str) -> tuple[OpenSeoKeyword, str] | None:
    """The most recent stored OpenSEO metrics for ``keyword`` in any project, and
    when they were fetched, for the advisor. The audit that just ran pulled for
    the audited site's project, so the newest row is that project's."""
    key = keyword.strip().lower()
    row = _latest(session, OpenSeoData.resource_key.endswith(f":{key}", autoescape=True))
    if row is None or str(row.payload.get("keyword", "")).lower() != key:
        return None
    return OpenSeoKeyword.model_validate(row.payload), row.fetched_at


def _latest(session: Session, key_matches: ColumnElement[bool]) -> OpenSeoData | None:
    return session.scalars(
        select(OpenSeoData)
        .where(OpenSeoData.resource_type == "keyword_volume", key_matches)
        .order_by(OpenSeoData.fetched_at.desc())
        .limit(1)
    ).first()


def _metrics_key(project_id: str, keyword: str) -> str:
    # The project sets the market (location and language), so the same keyword
    # in two projects is two different measurements.
    return f"{project_id}:{keyword.strip().lower()}"


def _response_message(resp: httpx.Response, msg_id: int) -> dict[str, Any]:
    """The JSON-RPC response with ``msg_id``, from a JSON body or an SSE stream."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for event in _sse_data(resp.text):
            try:
                message = json.loads(event)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("id") != msg_id:
                continue
            # A server request may reuse a numeric id; only a reply carries these.
            if "result" in message or "error" in message:
                return message
        raise ConnectorUnavailable("OpenSEO's event stream ended without a reply")
    try:
        message = resp.json()
    except ValueError as exc:
        raise ConnectorUnavailable("OpenSEO returned a reply that is neither JSON nor an event stream") from exc
    if not isinstance(message, dict):
        raise ConnectorUnavailable("OpenSEO returned an unexpected JSON-RPC reply")
    return message


def _sse_data(text: str) -> list[str]:
    """Each event's data, per the SSE spec: an event's ``data:`` lines join with
    newlines and a blank line ends the event, so a JSON body split across lines
    still arrives whole."""
    events: list[str] = []
    lines: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            value = line[5:]
            lines.append(value[1:] if value.startswith(" ") else value)
        elif not line and lines:
            events.append("\n".join(lines))
            lines = []
    return events


def _bare(domain: str) -> str:
    host = domain.lower().split("://", 1)[-1].split("/", 1)[0]
    return host.removeprefix("www.")


def _keyword(row: dict[str, Any]) -> OpenSeoKeyword:
    difficulty = row.get("keyword_difficulty")
    return OpenSeoKeyword(
        keyword=str(row.get("keyword", "")),
        monthly_volume=row.get("search_volume"),
        cpc_usd=row.get("cpc"),
        competition=row.get("competition"),
        keyword_difficulty=int(difficulty) if isinstance(difficulty, int | float) else None,
        intent=row.get("main_intent"),
    )
