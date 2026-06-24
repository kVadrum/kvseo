"""Advisor context assembly: bucketing, GSC ranking, PSI hydration, guards."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.engine import Engine

from kvseo.core.advisor.context import AdvisorError, build_context


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
