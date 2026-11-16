"""SerpBear connector — tracked-keyword rankings from a user-hosted SerpBear (03-connector-interfaces.md §5).

SerpBear tracks a list of keywords per domain and re-scrapes their positions on
its own schedule; kvseo only reads what it already has (``GET /api/keywords``
with the instance's ``APIKEY`` as a Bearer token), so a pull costs nothing and
triggers no scrape. Rankings persist to ``serpbear_rankings`` (07 §3.7).
"""

from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.base import ConnectorAuthError, ConnectorMeta, ConnectorUnavailable
from kvseo.storage.models import SerpBearRanking as SerpBearRankingORM
from kvseo.storage.timestamps import now_str

_TIMEOUT = httpx.Timeout(30.0)


class SerpBearHistoryPoint(BaseModel):
    date: str
    position: int | None


class SerpBearRanking(BaseModel):
    keyword: str
    domain: str
    location: str | None  # SerpBear's country code, e.g. 'US'
    device: str | None
    position: int | None  # None: not found within the tracked depth
    url: str | None
    checked_at: str  # SerpBear's lastUpdated
    history: list[SerpBearHistoryPoint]


class SerpBearConnector:
    """Read-only client for one SerpBear instance."""

    meta = ConnectorMeta(name="serpbear", version="0.1.0", capabilities=["rankings", "keywords"])

    def __init__(
        self,
        url: str,
        token: str,
        *,
        engine: Engine | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._token = token
        self._engine = engine
        self._client = client

    async def health_check(self) -> bool:
        await self.domains()
        return True

    async def domains(self) -> list[str]:
        """The domains this SerpBear tracks."""
        data = await self._get("/api/domains")
        return [str(d["domain"]) for d in data.get("domains") or [] if d.get("domain")]

    async def keywords(self, domain: str) -> list[SerpBearRanking]:
        """Every tracked keyword for ``domain`` with its current and past positions; persisted."""
        data = await self._get("/api/keywords", params={"domain": domain})
        rankings = [_ranking(item) for item in data.get("keywords") or []]
        self._persist(rankings)
        return rankings

    async def rankings(
        self, domain: str, keyword: str | None = None, location: str | None = None
    ) -> list[SerpBearRanking]:
        """Tracked rankings for ``domain``, optionally narrowed to one keyword and/or location."""
        found = await self.keywords(domain)
        if keyword is not None:
            found = [r for r in found if r.keyword.lower() == keyword.strip().lower()]
        if location is not None:
            found = [r for r in found if (r.location or "").lower() == location.lower()]
        return found

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            resp = await client.get(
                self._base + path, params=params, headers={"Authorization": f"Bearer {self._token}"}
            )
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(
                f"couldn't reach SerpBear at {self._base} ({exc}) — check the URL given to `kvseo connect serpbear`"
            ) from exc
        finally:
            if self._client is None:
                await client.aclose()
        if resp.status_code in (401, 403):
            raise ConnectorAuthError("SerpBear rejected the API key — re-run `kvseo connect serpbear`")
        if resp.status_code != 200:
            raise ConnectorUnavailable(f"SerpBear returned {resp.status_code} on {path}")
        try:
            data: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise ConnectorUnavailable(f"SerpBear returned non-JSON on {path} — is {self._base} a SerpBear?") from exc
        return data

    def _persist(self, rankings: list[SerpBearRanking]) -> None:
        if self._engine is not None:
            persist_rankings(self._engine, rankings)


def persist_rankings(engine: Engine, rankings: list[SerpBearRanking]) -> None:
    """Write one batch of rankings — the canonical write path, shared with the
    CSV ``rankings`` import so imported rows read exactly like pulled ones.
    One ``fetched_at`` per batch, because readers select the latest batch."""
    if not rankings:
        return
    fetched_at = now_str()
    with Session(engine) as session:
        session.add_all(
            SerpBearRankingORM(
                domain=r.domain,
                keyword=r.keyword,
                location=r.location,
                device=r.device,
                position=r.position,
                url=r.url,
                history=[p.model_dump() for p in r.history],
                checked_at=r.checked_at,
                fetched_at=fetched_at,
            )
            for r in rankings
        )
        session.commit()


def rankings_for_page(session: Session, page: str, keyword: str | None, *, limit: int) -> list[SerpBearRanking]:
    """The latest stored rankings that bear on ``page``: tracked keywords it ranks
    for, plus the audit's target keyword wherever it ranks. One coherent batch —
    the most recent pull for the page's domain."""
    domain = _bare_host(page)
    latest = session.scalar(
        select(func.max(SerpBearRankingORM.fetched_at)).where(SerpBearRankingORM.domain == domain)
    )
    if latest is None:
        return []
    rows = session.scalars(
        select(SerpBearRankingORM)
        .where(SerpBearRankingORM.domain == domain, SerpBearRankingORM.fetched_at == latest)
        .order_by(SerpBearRankingORM.keyword)
    ).all()
    target = (keyword or "").strip().lower()
    page_key = _page_key(page)
    relevant = [
        r for r in rows if (r.url and _page_key(r.url) == page_key) or (target and r.keyword.lower() == target)
    ]
    return [
        SerpBearRanking(
            keyword=r.keyword,
            domain=r.domain,
            location=r.location,
            device=r.device,
            position=r.position,
            url=r.url,
            checked_at=r.checked_at,
            history=[SerpBearHistoryPoint.model_validate(p) for p in r.history or []],
        )
        for r in relevant[:limit]
    ]


def serpbear_domain(url: str) -> str:
    """The domain SerpBear files ``url`` under: its host without ``www.``."""
    return _bare_host(url)


def _bare_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


def _page_key(url: str) -> str:
    # Rank trackers record the URL Google showed, which may differ from the
    # audited one by scheme, www, or a trailing slash.
    parsed = urlparse(url)
    return f"{_bare_host(url)}{parsed.path.rstrip('/') or '/'}"


def _ranking(item: dict[str, Any]) -> SerpBearRanking:
    position = item.get("position")
    history = item.get("history") or {}
    return SerpBearRanking(
        keyword=str(item.get("keyword", "")),
        domain=str(item.get("domain", "")),
        location=item.get("country"),
        device=item.get("device"),
        # SerpBear stores 0 for "not in the results it scraped".
        position=position if isinstance(position, int) and position > 0 else None,
        url=item.get("url") or None,
        checked_at=str(item.get("lastUpdated", "")),
        history=sorted(
            (
                SerpBearHistoryPoint(date=_iso_date(day), position=pos if isinstance(pos, int) and pos > 0 else None)
                for day, pos in history.items()
            ),
            key=lambda point: point.date,
        )
        if isinstance(history, dict)
        else [],
    )


def _iso_date(value: str) -> str:
    """SerpBear keys history by an unpadded ``YYYY-M-D`` (``2026-1-5``), which
    neither sorts nor parses as ISO; pad it. Anything else passes through."""
    try:
        year, month, day = (int(part) for part in value.split("-"))
        return date(year, month, day).isoformat()
    except ValueError:
        return value
