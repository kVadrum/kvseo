"""Google Search Console connector — data path (03-connector-interfaces.md §2).

Read-only Search Console REST calls over async httpx with a Bearer access
token. The OAuth flow + token refresh live in ``gsc_auth`` so this module stays
free of google-auth and is testable with a mock transport + a fake token.
Persists query rows to ``gsc_queries`` with a 24h freshness window
(02-architecture.md §6).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.base import (
    ConnectorAuthError,
    ConnectorMeta,
    ConnectorRateLimited,
    ConnectorUnavailable,
)
from kvseo.storage.models import GscQuery as GscQueryORM

_BASE = "https://www.googleapis.com/webmasters/v3"
_TIMEOUT = httpx.Timeout(30.0)
_DEFAULT_FRESHNESS = timedelta(days=1)
_MAX_CONCURRENCY = 10  # self-imposed, leaves headroom under GSC's 30 QPS cap
_SQLITE_TS = "%Y-%m-%d %H:%M:%S"


class GscSite(BaseModel):
    site_url: str
    permission_level: str


class GscQueryRow(BaseModel):
    query: str
    page: str
    clicks: int
    impressions: int
    ctr: float  # 0.0-1.0
    position: float  # average position
    date_range_start: date
    date_range_end: date


class GscConnector:
    """Read-only Google Search Console connector.

    ``access_token`` is a Bearer token (obtained + refreshed by ``gsc_auth``);
    ``client``/``engine`` are injectable for tests and persistence respectively.
    """

    meta = ConnectorMeta(name="gsc", version="0.1.0", capabilities=["queries", "pages", "sites"])

    def __init__(
        self,
        *,
        access_token: str | None = None,
        engine: Engine | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = access_token
        self._engine = engine
        self._client = client
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def sites(self) -> list[GscSite]:
        """List the GSC properties the connected account can access."""
        data = await self._request("GET", "/sites")
        return [
            GscSite(site_url=entry["siteUrl"], permission_level=entry.get("permissionLevel", ""))
            for entry in data.get("siteEntry", [])
        ]

    async def queries_for_url(
        self,
        site: str,
        url: str,
        start: date,
        end: date,
        *,
        row_limit: int = 1000,
        refresh: bool = False,
        freshness: timedelta = _DEFAULT_FRESHNESS,
    ) -> list[GscQueryRow]:
        """Per-query metrics for a single page over a date range."""
        if not refresh and self._engine is not None:
            cached = self._cached_for_url(site, url, start, end, freshness)
            if cached is not None:
                return cached
        raw = await self._search_analytics(
            site, start, end, dimensions=["query"], row_limit=row_limit, page_filter=url
        )
        rows = [
            GscQueryRow(
                query=r["keys"][0],
                page=url,
                clicks=r["clicks"],
                impressions=r["impressions"],
                ctr=r["ctr"],
                position=r["position"],
                date_range_start=start,
                date_range_end=end,
            )
            for r in raw
        ]
        self._persist(site, rows)
        return rows

    async def queries_for_site(
        self,
        site: str,
        start: date,
        end: date,
        *,
        row_limit: int = 25000,
    ) -> list[GscQueryRow]:
        """Per-query metrics across an entire site (query x page dimensions)."""
        raw = await self._search_analytics(
            site, start, end, dimensions=["query", "page"], row_limit=row_limit
        )
        rows = [
            GscQueryRow(
                query=r["keys"][0],
                page=r["keys"][1],
                clicks=r["clicks"],
                impressions=r["impressions"],
                ctr=r["ctr"],
                position=r["position"],
                date_range_start=start,
                date_range_end=end,
            )
            for r in raw
        ]
        self._persist(site, rows)
        return rows

    async def health_check(self) -> bool:
        """Confirm the token works by listing sites."""
        await self.sites()
        return True

    # --- HTTP -------------------------------------------------------------

    async def _search_analytics(
        self,
        site: str,
        start: date,
        end: date,
        *,
        dimensions: list[str],
        row_limit: int,
        page_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dimensions,
            "rowLimit": row_limit,
        }
        if page_filter is not None:
            body["dimensionFilterGroups"] = [
                {
                    "filters": [
                        {"dimension": "page", "operator": "equals", "expression": page_filter}
                    ]
                }
            ]
        path = f"/sites/{quote(site, safe='')}/searchAnalytics/query"
        data = await self._request("POST", path, json_body=body)
        rows = data.get("rows", [])
        return rows if isinstance(rows, list) else []

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not self._token:
            raise ConnectorAuthError("GSC is not connected — run `kvseo connect gsc`")
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with self._semaphore:
                resp = await client.request(method, _BASE + path, headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            raise ConnectorUnavailable(f"GSC timed out on {path}") from exc
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(f"GSC request failed on {path}: {exc}") from exc
        finally:
            if self._client is None:
                await client.aclose()
        self._raise_for_status(resp)
        return resp.json()  # type: ignore[no-any-return]

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        code = resp.status_code
        if code == 200:
            return
        if code in (401, 403):
            raise ConnectorAuthError(
                f"GSC auth failed ({code}) — re-run `kvseo connect gsc`"
                + (" (property may not be granted)" if code == 403 else "")
            )
        if code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise ConnectorRateLimited(retry_after, "GSC quota exceeded")
        if code >= 500:
            raise ConnectorUnavailable(f"GSC server error ({code})")
        raise ConnectorUnavailable(f"GSC returned unexpected status {code}")

    # --- Persistence + freshness -----------------------------------------

    def _persist(self, site: str, rows: list[GscQueryRow]) -> None:
        if self._engine is None or not rows:
            return
        # Stamp every row in the batch with one timestamp computed once, rather
        # than the per-row ``datetime('now')`` server default: _cached_for_url
        # reads back the rows matching max(fetched_at), so a batch that straddled
        # a 1-second boundary would otherwise return only its last-second subset.
        fetched_at = datetime.now(UTC).strftime(_SQLITE_TS)
        with Session(self._engine) as session:
            session.add_all(
                GscQueryORM(
                    site_origin=site,
                    page=r.page,
                    query=r.query,
                    clicks=r.clicks,
                    impressions=r.impressions,
                    ctr=r.ctr,
                    position=r.position,
                    range_start=r.date_range_start.isoformat(),
                    range_end=r.date_range_end.isoformat(),
                    fetched_at=fetched_at,
                )
                for r in rows
            )
            session.commit()

    def _cached_for_url(
        self, site: str, url: str, start: date, end: date, freshness: timedelta
    ) -> list[GscQueryRow] | None:
        assert self._engine is not None
        # The cache key includes the requested date range: the same URL fetched
        # for a different period must miss, not hand back the prior range's rows
        # mislabeled as the requested one.
        range_start, range_end = start.isoformat(), end.isoformat()
        with Session(self._engine) as session:
            latest = session.scalar(
                select(func.max(GscQueryORM.fetched_at)).where(
                    GscQueryORM.page == url,
                    GscQueryORM.site_origin == site,
                    GscQueryORM.range_start == range_start,
                    GscQueryORM.range_end == range_end,
                )
            )
            if latest is None:
                return None
            fetched = datetime.strptime(latest, _SQLITE_TS).replace(tzinfo=UTC)
            if datetime.now(UTC) - fetched >= freshness:
                return None
            rows = session.scalars(
                select(GscQueryORM).where(
                    GscQueryORM.page == url,
                    GscQueryORM.site_origin == site,
                    GscQueryORM.range_start == range_start,
                    GscQueryORM.range_end == range_end,
                    GscQueryORM.fetched_at == latest,
                )
            ).all()
        return [
            GscQueryRow(
                query=r.query,
                page=r.page,
                clicks=r.clicks,
                impressions=r.impressions,
                ctr=r.ctr,
                position=r.position,
                date_range_start=date.fromisoformat(r.range_start),
                date_range_end=date.fromisoformat(r.range_end),
            )
            for r in rows
        ]
