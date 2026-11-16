"""DataForSEO connector — keyword volume, keyword ideas, live SERPs (03-connector-interfaces.md §6).

BYO account over HTTP Basic auth; kvseo never proxies. DataForSEO answers
HTTP 200 even when a call failed and puts the real outcome in a body
``status_code`` (20000 = ok, 40xxx = auth/billing/request, 50xxx = server), on
the envelope and again per task, so both are checked and mapped onto the
connector error taxonomy. Every call is billed: the ``cost`` DataForSEO reports
is stored with each persisted row in ``dataforseo_data`` (07 §3.7), and keyword
data is served from that cache for a week (02 §6) so re-auditing a page does
not pay twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.base import (
    ConnectorAuthError,
    ConnectorMeta,
    ConnectorUnavailable,
)
from kvseo.storage.models import DataForSeoData
from kvseo.storage.timestamps import now_str, parse_ts

LIVE_URL = "https://api.dataforseo.com/v3"
SANDBOX_URL = "https://sandbox.dataforseo.com/v3"
US = 2840  # DataForSEO location_code for the United States
_TIMEOUT = httpx.Timeout(60.0)  # live SERP tasks routinely take 10-30s
_KEYWORD_FRESHNESS = timedelta(days=7)
_SERP_FRESHNESS = timedelta(days=1)
_OK = 20000


class MonthlyVolume(BaseModel):
    year: int
    month: int
    search_volume: int | None


class DfsKeywordVolume(BaseModel):
    keyword: str
    location_code: int
    language_code: str
    search_volume: int | None
    cpc_usd: float | None
    competition: float | None  # 0.0-1.0 (Google Ads competition index / 100)
    monthly_searches: list[MonthlyVolume]
    fetched_at: datetime


class DfsKeywordIdea(BaseModel):
    keyword: str
    search_volume: int | None
    cpc_usd: float | None
    competition: float | None
    keyword_difficulty: int | None
    intent: str | None


class DfsSerpResult(BaseModel):
    rank: int  # rank_group: position among organic results
    domain: str | None
    url: str | None
    title: str | None


class DfsSerp(BaseModel):
    keyword: str
    location_code: int
    language_code: str
    results: list[DfsSerpResult]
    fetched_at: datetime


class DataForSeoConnector:
    """Read-only DataForSEO client. ``client``/``engine`` are injectable."""

    meta = ConnectorMeta(
        name="dataforseo", version="0.1.0", capabilities=["keyword_volume", "keyword_ideas", "serp"]
    )

    def __init__(
        self,
        login: str,
        password: str,
        *,
        sandbox: bool = False,
        engine: Engine | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._auth = httpx.BasicAuth(login, password)
        self._base = SANDBOX_URL if sandbox else LIVE_URL
        self._engine = engine
        self._client = client

    async def health_check(self) -> bool:
        """Confirm the credentials work. ``appendix/user_data`` is free to call."""
        await self._request("GET", "/appendix/user_data")
        return True

    async def keyword_volume(
        self,
        keywords: list[str],
        location_code: int = US,
        language_code: str = "en",
        *,
        refresh: bool = False,
    ) -> list[DfsKeywordVolume]:
        """Google Ads search volume, CPC and competition for each keyword.

        Keywords cached within a week are served from storage; only the rest are
        billed, in one task.
        """
        wanted = list(dict.fromkeys(k.strip().lower() for k in keywords if k.strip()))
        cached = {} if refresh else self._cached_volumes(wanted, location_code, language_code)
        missing = [k for k in wanted if k not in cached]
        if missing:
            body = [{"keywords": missing, "location_code": location_code, "language_code": language_code}]
            result, cost = await self._task("/keywords_data/google_ads/search_volume/live", body)
            fetched_at = datetime.now(UTC)
            fresh = [_volume(item, location_code, language_code, fetched_at) for item in result]
            if self._engine is not None:
                persist_keyword_volumes(self._engine, fresh, cost=cost, source="api")
            cached.update({v.keyword.lower(): v for v in fresh})
        return [cached[k] for k in wanted if k in cached]

    async def keyword_ideas(
        self, seed: str, limit: int = 100, location_code: int = US, language_code: str = "en"
    ) -> list[DfsKeywordIdea]:
        """Related keyword ideas for a seed (DataForSEO Labs)."""
        body = [{"keywords": [seed], "location_code": location_code, "language_code": language_code, "limit": limit}]
        result, cost = await self._task("/dataforseo_labs/google/keyword_ideas/live", body)
        items = (result[0].get("items") if result else None) or []
        ideas = [_idea(item) for item in items]
        self._persist(
            "keyword_ideas",
            [(f"{location_code}:{language_code}:{seed.strip().lower()}", [i.model_dump() for i in ideas])],
            cost,
        )
        return ideas

    async def serp(self, keyword: str, location_code: int = US, language_code: str = "en") -> DfsSerp:
        """The live top-10 Google organic results for a keyword."""
        key = _volume_key(keyword, location_code, language_code)
        cached = self._latest("serp", key, _SERP_FRESHNESS)
        if cached is not None:
            return DfsSerp.model_validate(cached)
        body = [{"keyword": keyword, "location_code": location_code, "language_code": language_code, "depth": 10}]
        result, cost = await self._task("/serp/google/organic/live/advanced", body)
        items = (result[0].get("items") if result else None) or []
        serp = DfsSerp(
            keyword=keyword,
            location_code=location_code,
            language_code=language_code,
            results=[
                DfsSerpResult(
                    rank=int(item.get("rank_group") or 0),
                    domain=item.get("domain"),
                    url=item.get("url"),
                    title=item.get("title"),
                )
                for item in items
                if item.get("type") == "organic"
            ],
            fetched_at=datetime.now(UTC),
        )
        self._persist("serp", [(key, serp.model_dump(mode="json"))], cost)
        return serp

    # --- HTTP -------------------------------------------------------------

    async def _task(self, path: str, body: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float | None]:
        """POST one task; return its ``result`` list and the billed cost."""
        data = await self._request("POST", path, json_body=body)
        tasks = data.get("tasks") or []
        if not tasks:
            raise ConnectorUnavailable(f"DataForSEO returned no task for {path}")
        task = tasks[0]
        _raise_for_status(task.get("status_code"), task.get("status_message"))
        cost = data.get("cost")
        return task.get("result") or [], float(cost) if isinstance(cost, int | float) else None

    async def _request(
        self, method: str, path: str, *, json_body: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            resp = await client.request(method, self._base + path, json=json_body, auth=self._auth)
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(f"DataForSEO request failed on {path}: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code in (401, 403):
            raise ConnectorAuthError("DataForSEO rejected the login — re-run `kvseo connect dataforseo`")
        if resp.status_code >= 500:
            raise ConnectorUnavailable(f"DataForSEO server error ({resp.status_code})")
        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise ConnectorUnavailable(f"DataForSEO returned non-JSON ({resp.status_code})") from exc
        _raise_for_status(data.get("status_code"), data.get("status_message"))
        return data

    # --- Persistence + freshness -----------------------------------------

    def _persist(self, resource_type: str, rows: list[tuple[str, Any]], cost: float | None) -> None:
        if self._engine is None or not rows:
            return
        # The call's cost is split across the rows it produced, so summing the
        # table reports what DataForSEO actually billed.
        share = cost / len(rows) if cost is not None else None
        fetched_at = now_str()
        with Session(self._engine) as session:
            session.add_all(
                DataForSeoData(
                    resource_type=resource_type,
                    resource_key=key,
                    payload=payload,
                    estimated_cost_usd=share,
                    fetched_at=fetched_at,
                )
                for key, payload in rows
            )
            session.commit()

    def _latest(self, resource_type: str, key: str, freshness: timedelta) -> Any:
        if self._engine is None:
            return None
        with Session(self._engine) as session:
            row = session.scalars(
                select(DataForSeoData)
                .where(DataForSeoData.resource_type == resource_type, DataForSeoData.resource_key == key)
                .order_by(DataForSeoData.fetched_at.desc())
                .limit(1)
            ).first()
        if row is None or datetime.now(UTC) - parse_ts(row.fetched_at) > freshness:
            return None
        return row.payload

    def _cached_volumes(
        self, keywords: list[str], location_code: int, language_code: str
    ) -> dict[str, DfsKeywordVolume]:
        found: dict[str, DfsKeywordVolume] = {}
        for keyword in keywords:
            key = _volume_key(keyword, location_code, language_code)
            payload = self._latest("keyword_volume", key, _KEYWORD_FRESHNESS)
            if payload is not None:
                found[keyword] = DfsKeywordVolume.model_validate(payload)
        return found


def persist_keyword_volumes(
    engine: Engine, volumes: list[DfsKeywordVolume], *, cost: float | None, source: str
) -> None:
    """Write keyword-volume rows — the canonical write path, shared with the CSV
    ``keywords`` import. ``source`` records where the numbers came from ('api' or
    'csv'), since both land in this table in the same shape."""
    if not volumes:
        return
    # A call's cost is split across the rows it produced, so summing the table
    # reports what DataForSEO actually billed.
    share = cost / len(volumes) if cost is not None else None
    fetched_at = now_str()
    with Session(engine) as session:
        session.add_all(
            DataForSeoData(
                resource_type="keyword_volume",
                resource_key=_volume_key(v.keyword, v.location_code, v.language_code),
                payload=v.model_dump(mode="json"),
                estimated_cost_usd=share,
                source=source,
                fetched_at=fetched_at,
            )
            for v in volumes
        )
        session.commit()


def latest_keyword_volume(
    session: Session, keyword: str, location_code: int = US, language_code: str = "en"
) -> tuple[DfsKeywordVolume, str] | None:
    """The most recent stored volume row for ``keyword`` and its source ('api' or
    'csv'), for the advisor."""
    row = session.scalars(
        select(DataForSeoData)
        .where(
            DataForSeoData.resource_type == "keyword_volume",
            DataForSeoData.resource_key == _volume_key(keyword, location_code, language_code),
        )
        .order_by(DataForSeoData.fetched_at.desc())
        .limit(1)
    ).first()
    return (DfsKeywordVolume.model_validate(row.payload), row.source) if row is not None else None


def _volume_key(keyword: str, location_code: int, language_code: str) -> str:
    return f"{location_code}:{language_code}:{keyword.strip().lower()}"


def _raise_for_status(code: Any, message: Any) -> None:
    if code == _OK or code is None:
        return
    detail = f"DataForSEO {code}: {message or 'no message'}"
    if isinstance(code, int) and 40100 <= code < 40300:
        # 401xx: credentials; 402xx: balance or plan limits. Both need the user.
        raise ConnectorAuthError(detail)
    raise ConnectorUnavailable(detail)


def _volume(item: dict[str, Any], location_code: int, language_code: str, fetched_at: datetime) -> DfsKeywordVolume:
    index = item.get("competition_index")
    return DfsKeywordVolume(
        keyword=str(item.get("keyword", "")),
        location_code=int(item.get("location_code") or location_code),
        language_code=str(item.get("language_code") or language_code),
        search_volume=item.get("search_volume"),
        cpc_usd=item.get("cpc"),
        competition=index / 100 if isinstance(index, int | float) else None,
        monthly_searches=[
            MonthlyVolume(year=m["year"], month=m["month"], search_volume=m.get("search_volume"))
            for m in item.get("monthly_searches") or []
        ],
        fetched_at=fetched_at,
    )


def _idea(item: dict[str, Any]) -> DfsKeywordIdea:
    info = item.get("keyword_info") or {}
    props = item.get("keyword_properties") or {}
    intent = item.get("search_intent_info") or {}
    return DfsKeywordIdea(
        keyword=str(item.get("keyword", "")),
        search_volume=info.get("search_volume"),
        cpc_usd=info.get("cpc"),
        competition=info.get("competition"),
        keyword_difficulty=props.get("keyword_difficulty"),
        intent=intent.get("main_intent"),
    )
