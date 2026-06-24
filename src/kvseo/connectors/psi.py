"""PageSpeed Insights connector (03-connector-interfaces.md §3).

Fetches Core Web Vitals (CrUX field data) + Lighthouse lab metrics for a URL,
persists to ``psi_results``, and serves cached results within a freshness
window (default 1h, 02-architecture.md §6). API-key auth raises the free-tier
quota to 25k/day; PSI also works keyless at lower limits.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.base import (
    ConnectorAuthError,
    ConnectorMeta,
    ConnectorRateLimited,
    ConnectorUnavailable,
)
from kvseo.storage.models import PsiResult as PsiResultORM

_ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
_TIMEOUT = httpx.Timeout(30.0)
_DEFAULT_FRESHNESS = timedelta(hours=1)
_MAX_OPPORTUNITIES = 10
# SQLite datetime('now') format — naive UTC, no fractional seconds.
_SQLITE_TS = "%Y-%m-%d %H:%M:%S"

Strategy = Literal["mobile", "desktop"]


class PsiOpportunity(BaseModel):
    id: str  # Lighthouse audit id, e.g. 'unused-css-rules'
    title: str
    description: str
    savings_ms: int
    score: float  # 0.0-1.0


class PsiResult(BaseModel):
    url: str
    strategy: str
    fetched_at: datetime
    # Field data (real Chrome user data, when available)
    field_lcp_ms: int | None
    field_inp_ms: int | None
    field_cls: float | None
    field_origin_fallback: bool  # True when only origin-level field data exists
    # Lab data (Lighthouse synthetic run)
    lab_lcp_ms: int
    lab_tbt_ms: int
    lab_cls: float
    lab_performance_score: int  # 0-100
    opportunities: list[PsiOpportunity] = Field(default_factory=list)


class PsiConnector:
    """Read-only PageSpeed Insights connector.

    ``client`` is injectable for tests (an ``httpx.AsyncClient`` over a mock
    transport); in production each call opens its own client. ``engine`` enables
    persistence + freshness; without it the connector still fetches and returns.
    """

    meta = ConnectorMeta(name="psi", version="0.1.0", capabilities=["cwv", "lighthouse"])

    def __init__(
        self,
        *,
        api_key: str | None = None,
        engine: Engine | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._engine = engine
        self._client = client

    async def core_web_vitals(
        self,
        url: str,
        strategy: Strategy = "mobile",
        *,
        refresh: bool = False,
        freshness: timedelta = _DEFAULT_FRESHNESS,
    ) -> PsiResult:
        """Fetch CWV + Lighthouse for a URL, or return a fresh cached result."""
        if not refresh and self._engine is not None:
            cached = self._cached(url, strategy, freshness)
            if cached is not None:
                return cached
        payload = await self._fetch(url, strategy)
        result = self._parse(url, strategy, payload)
        self._persist(result, payload)
        return result

    async def health_check(self) -> bool:
        """Confirm the key + connectivity with one lightweight live call."""
        await self._fetch("https://example.com", "mobile")
        return True

    # --- HTTP -------------------------------------------------------------

    async def _fetch(self, url: str, strategy: Strategy) -> dict[str, Any]:
        params: dict[str, str] = {"url": url, "strategy": strategy, "category": "performance"}
        if self._api_key:
            params["key"] = self._api_key
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            resp = await client.get(_ENDPOINT, params=params)
        except httpx.TimeoutException as exc:
            raise ConnectorUnavailable(f"PSI timed out for {url}") from exc
        except httpx.HTTPError as exc:
            raise ConnectorUnavailable(f"PSI request failed for {url}: {exc}") from exc
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
            raise ConnectorAuthError(f"PSI auth failed ({code}) — check your API key")
        if code == 429:
            retry_after = int(resp.headers.get("Retry-After", "60"))
            raise ConnectorRateLimited(retry_after, "PSI rate limit hit")
        if code >= 500:
            raise ConnectorUnavailable(f"PSI server error ({code})")
        raise ConnectorUnavailable(f"PSI returned unexpected status {code}")

    # --- Parsing ----------------------------------------------------------

    def _parse(self, url: str, strategy: str, data: dict[str, Any]) -> PsiResult:
        field = data.get("loadingExperience", {})
        metrics = field.get("metrics", {})
        cls_pct = metrics.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile")

        lighthouse = data.get("lighthouseResult")
        if not lighthouse:
            # Field-only responses can't fill the required lab metrics; degrade.
            raise ConnectorUnavailable("PSI returned no Lighthouse data")
        audits = lighthouse["audits"]

        return PsiResult(
            url=url,
            strategy=strategy,
            fetched_at=datetime.now(UTC),
            field_lcp_ms=metrics.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile"),
            field_inp_ms=metrics.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile"),
            # CrUX reports the CLS percentile x100 (e.g. 8 -> 0.08).
            field_cls=cls_pct / 100 if cls_pct is not None else None,
            field_origin_fallback=bool(field.get("origin_fallback", False)),
            lab_lcp_ms=round(audits["largest-contentful-paint"]["numericValue"]),
            lab_tbt_ms=round(audits["total-blocking-time"]["numericValue"]),
            lab_cls=audits["cumulative-layout-shift"]["numericValue"],
            lab_performance_score=round(lighthouse["categories"]["performance"]["score"] * 100),
            opportunities=self._parse_opportunities(audits),
        )

    @staticmethod
    def _parse_opportunities(audits: dict[str, Any]) -> list[PsiOpportunity]:
        opportunities: list[PsiOpportunity] = []
        for audit_id, audit in audits.items():
            details = audit.get("details", {})
            if details.get("type") != "opportunity":
                continue
            savings = details.get("overallSavingsMs", 0)
            if not savings:
                continue
            opportunities.append(
                PsiOpportunity(
                    id=audit_id,
                    title=audit.get("title", ""),
                    description=audit.get("description", ""),
                    savings_ms=round(savings),
                    score=audit.get("score") or 0.0,
                )
            )
        opportunities.sort(key=lambda o: o.savings_ms, reverse=True)
        return opportunities[:_MAX_OPPORTUNITIES]

    # --- Persistence + freshness -----------------------------------------

    def _persist(self, result: PsiResult, raw: dict[str, Any]) -> None:
        if self._engine is None:
            return
        with Session(self._engine) as session:
            session.add(
                PsiResultORM(
                    url=result.url,
                    strategy=result.strategy,
                    field_lcp_ms=result.field_lcp_ms,
                    field_inp_ms=result.field_inp_ms,
                    field_cls=result.field_cls,
                    field_origin_fallback=result.field_origin_fallback,
                    lab_lcp_ms=result.lab_lcp_ms,
                    lab_tbt_ms=result.lab_tbt_ms,
                    lab_cls=result.lab_cls,
                    lab_performance_score=result.lab_performance_score,
                    opportunities=[o.model_dump() for o in result.opportunities],
                    raw_response=json.dumps(raw, separators=(",", ":")),
                )
            )
            session.commit()

    def _cached(self, url: str, strategy: str, freshness: timedelta) -> PsiResult | None:
        assert self._engine is not None
        with Session(self._engine) as session:
            row = session.scalars(
                select(PsiResultORM)
                .where(PsiResultORM.url == url, PsiResultORM.strategy == strategy)
                .order_by(PsiResultORM.fetched_at.desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        fetched = datetime.strptime(row.fetched_at, _SQLITE_TS).replace(tzinfo=UTC)
        if datetime.now(UTC) - fetched >= freshness:
            return None
        return self._orm_to_result(row, fetched)

    @staticmethod
    def _orm_to_result(row: PsiResultORM, fetched: datetime) -> PsiResult:
        opportunities = [PsiOpportunity(**o) for o in (row.opportunities or [])]
        return PsiResult(
            url=row.url,
            strategy=row.strategy,
            fetched_at=fetched,
            field_lcp_ms=row.field_lcp_ms,
            field_inp_ms=row.field_inp_ms,
            field_cls=row.field_cls,
            field_origin_fallback=row.field_origin_fallback,
            lab_lcp_ms=row.lab_lcp_ms,
            lab_tbt_ms=row.lab_tbt_ms,
            lab_cls=row.lab_cls,
            lab_performance_score=row.lab_performance_score,
            opportunities=opportunities,
        )
