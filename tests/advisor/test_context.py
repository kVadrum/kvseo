"""Advisor context assembly: bucketing, GSC ranking, PSI hydration, guards."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.core.advisor.context import AdvisorError, build_context
from kvseo.storage.models import DataForSeoData, OpenSeoData, SerpBearRanking


def test_buckets_checks_by_verdict(audit_engine: Engine, seed: Callable[..., uuid.UUID]) -> None:
    aid = seed(audit_engine)
    ctx = build_context(aid, audit_engine)

    assert ctx.audit.url == "https://kemek.net/services"
    assert ctx.audit.score == 72
    assert [c.id for c in ctx.audit.failed] == ["meta.canonical"]
    assert [c.id for c in ctx.audit.warned] == ["title.length"]
    assert set(ctx.audit.passed_ids) == {"headings.h1", "content.wordcount"}
    assert ctx.audit.not_assessed_ids == ["cwv.lcp"]  # the skipped check
    assert ctx.target_keyword == "ops consulting"


def test_gsc_queries_ranked_by_impressions(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    ctx = build_context(aid, audit_engine)
    impressions = [q.impressions for q in ctx.gsc_queries]
    assert impressions == sorted(impressions, reverse=True)
    assert ctx.gsc_queries[0].query == "ops consulting west virginia"


def test_gsc_limit_is_respected(audit_engine: Engine, seed: Callable[..., uuid.UUID]) -> None:
    aid = seed(audit_engine)
    ctx = build_context(aid, audit_engine, max_gsc_queries=2)
    assert len(ctx.gsc_queries) == 2


def test_psi_is_hydrated(audit_engine: Engine, seed: Callable[..., uuid.UUID]) -> None:
    aid = seed(audit_engine)
    ctx = build_context(aid, audit_engine)
    assert ctx.psi is not None
    assert ctx.psi.field_lcp_ms == 3120
    assert ctx.psi.lab_performance_score == 68
    assert ctx.psi.opportunities[0].id == "unused-css-rules"


def test_no_connector_data_is_empty_not_error(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine, with_gsc=False, with_psi=False)
    ctx = build_context(aid, audit_engine)
    assert ctx.gsc_queries == []
    assert ctx.psi is None


def test_missing_audit_raises(audit_engine: Engine) -> None:
    with pytest.raises(AdvisorError, match="not found"):
        build_context(uuid.uuid4(), audit_engine)


def test_incomplete_audit_raises(audit_engine: Engine, seed: Callable[..., uuid.UUID]) -> None:
    aid = seed(audit_engine, status="running")
    with pytest.raises(AdvisorError, match="not 'complete'"):
        build_context(aid, audit_engine)


def _store_v02_rows(engine: Engine) -> None:
    """A DataForSEO volume row for the seeded keyword, an OpenSEO row for the
    same keyword, and one SerpBear batch for kemek.net."""
    with Session(engine) as s:
        s.add_all(
            [
                DataForSeoData(
                    resource_type="keyword_volume",
                    resource_key="2840:en:ops consulting",
                    payload={
                        "keyword": "ops consulting", "location_code": 2840, "language_code": "en",
                        "search_volume": 880, "cpc_usd": 9.5, "competition": 0.3,
                        "monthly_searches": [], "fetched_at": "2026-11-16T00:00:00Z",
                    },
                    estimated_cost_usd=0.075,
                ),
                OpenSeoData(
                    resource_type="keyword_volume",
                    resource_key="ops consulting",
                    payload={"keyword": "ops consulting", "monthly_volume": 700, "cpc_usd": None,
                             "competition": None, "keyword_difficulty": 33, "intent": "commercial"},
                ),
                SerpBearRanking(
                    domain="kemek.net", keyword="ops consulting", location="US", device="desktop",
                    position=14, url="https://kemek.net/services/",
                    history=[{"date": f"2026-10-{d:02d}", "position": 20} for d in range(1, 31)],
                    checked_at="2026-11-15", fetched_at="2026-11-16 00:00:00",
                ),
                SerpBearRanking(
                    domain="kemek.net", keyword="kemek network", location="US", device="desktop",
                    position=1, url="https://kemek.net/", history=[],
                    checked_at="2026-11-15", fetched_at="2026-11-16 00:00:00",
                ),
            ]
        )
        s.commit()


def test_v02_sources_reach_the_context_and_resolve_as_evidence(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    _store_v02_rows(audit_engine)
    ctx = build_context(aid, audit_engine)

    # DataForSEO wins over OpenSEO for the same keyword, as the audit's pull does.
    assert [(m.source, m.monthly_volume) for m in ctx.keyword_metrics] == [("dataforseo", 880)]
    # Only rankings bearing on this page or the target keyword; history trimmed.
    assert [r.keyword for r in ctx.serpbear_rankings] == ["ops consulting"]
    assert len(ctx.serpbear_rankings[0].history) == 10
    index = ctx.evidence_index()
    assert index.resolves("serpbear_rankings[0]")
    assert index.resolves("keyword_metrics[0]")
    assert not index.resolves("serpbear_rankings[1]")


def test_openseo_metrics_stand_in_when_dataforseo_has_none(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    with Session(audit_engine) as s:
        s.add(OpenSeoData(resource_type="keyword_volume", resource_key="ops consulting",
                          payload={"keyword": "ops consulting", "monthly_volume": 700, "cpc_usd": None,
                                   "competition": None, "keyword_difficulty": 33, "intent": "commercial"}))
        s.commit()
    metrics = build_context(aid, audit_engine).keyword_metrics
    assert [(m.source, m.keyword_difficulty, m.intent) for m in metrics] == [("openseo", 33, "commercial")]
