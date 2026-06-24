"""Audit checks against a good page and a poor page (04-audit-engine.md §2).

One shared fixture per intent (R16: reuse fixtures across checks) plus targeted
cases for https scheme and CWV.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kvseo.connectors.psi import PsiResult
from kvseo.core.audit.checks import content, cwv, headings, meta, title
from kvseo.core.audit.checks._base import AuditContext
from kvseo.core.audit.document import ParsedDocument

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "html"
_BASE = "https://example.com/"


@pytest.fixture
def good() -> ParsedDocument:
    return ParsedDocument((_FIXTURES / "good.html").read_text(), _BASE)


@pytest.fixture
def poor() -> ParsedDocument:
    return ParsedDocument((_FIXTURES / "poor.html").read_text(), _BASE)


def _ctx(keyword: str | None = None, *, url: str = _BASE) -> AuditContext:
    return AuditContext(fetched_url=url, keyword=keyword)


def test_title_checks(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert title.title_presence(good, _ctx()).verdict == "pass"
    assert title.title_presence(poor, _ctx()).verdict == "fail"
    assert title.title_length(good, _ctx()).verdict == "pass"
    assert title.title_length(poor, _ctx()).verdict == "skip"  # no title to measure
    assert title.title_keyword(good, _ctx("seo audit")).verdict == "pass"
    assert title.title_keyword(good, _ctx()).verdict == "skip"  # no keyword set


def test_meta_checks(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert meta.description_presence(good, _ctx()).verdict == "pass"
    assert meta.description_presence(poor, _ctx()).verdict == "fail"
    assert meta.description_length(good, _ctx()).verdict == "pass"
    assert meta.robots(good, _ctx()).verdict == "pass"
    assert meta.robots(poor, _ctx()).verdict == "fail"  # noindex
    assert meta.canonical(good, _ctx()).verdict == "pass"
    assert meta.canonical(poor, _ctx()).verdict == "warn"
    assert meta.open_graph(good, _ctx()).verdict == "pass"
    assert meta.open_graph(poor, _ctx()).verdict == "warn"  # only og:title


def test_heading_checks(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert headings.h1_presence(good, _ctx()).verdict == "pass"
    assert headings.h1_presence(poor, _ctx()).verdict == "warn"  # two h1s
    assert headings.hierarchy(good, _ctx()).verdict == "pass"
    skipped = headings.hierarchy(poor, _ctx())
    assert skipped.verdict == "warn"  # h1 -> h3 skips h2
    assert skipped.data["skips"] == [{"from": 1, "to": 3}]


def test_content_checks(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert content.viewport(good, _ctx()).verdict == "pass"
    assert content.viewport(poor, _ctx()).verdict == "warn"
    assert content.language(good, _ctx()).verdict == "pass"
    assert content.language(poor, _ctx()).verdict == "warn"
    assert content.images_alt(good, _ctx()).verdict == "pass"
    assert content.images_alt(poor, _ctx()).verdict == "warn"
    assert content.internal_links_count(good, _ctx()).verdict == "pass"  # 3 internal
    assert content.internal_links_count(poor, _ctx()).verdict == "warn"  # 1 internal
    assert content.schema_presence(good, _ctx()).verdict == "pass"
    assert content.schema_presence(poor, _ctx()).verdict == "warn"


def test_https_check(good: ParsedDocument) -> None:
    assert content.https(good, _ctx(url="https://example.com/")).verdict == "pass"
    assert content.https(good, _ctx(url="http://example.com/")).verdict == "fail"


def _psi(*, lcp: int, inp: int, cls: float) -> PsiResult:
    return PsiResult(
        url=_BASE, strategy="mobile", fetched_at=datetime.now(UTC),
        field_lcp_ms=lcp, field_inp_ms=inp, field_cls=cls, field_origin_fallback=False,
        lab_lcp_ms=lcp, lab_tbt_ms=100, lab_cls=cls, lab_performance_score=90,
    )


def test_cwv_checks(good: ParsedDocument) -> None:
    healthy = AuditContext(fetched_url=_BASE, psi_result=_psi(lcp=2000, inp=150, cls=0.05))
    assert cwv.cwv_lcp(good, healthy).verdict == "pass"
    assert cwv.cwv_inp(good, healthy).verdict == "pass"
    assert cwv.cwv_cls(good, healthy).verdict == "pass"

    bad = AuditContext(fetched_url=_BASE, psi_result=_psi(lcp=3200, inp=300, cls=0.3))
    assert cwv.cwv_lcp(good, bad).verdict == "fail"
    assert cwv.cwv_cls(good, bad).verdict == "fail"

    # Google's "good" bands are inclusive: a value exactly on the boundary passes.
    boundary = AuditContext(fetched_url=_BASE, psi_result=_psi(lcp=2500, inp=200, cls=0.1))
    assert cwv.cwv_lcp(good, boundary).verdict == "pass"
    assert cwv.cwv_inp(good, boundary).verdict == "pass"
    assert cwv.cwv_cls(good, boundary).verdict == "pass"

    # No PSI → cwv checks skip.
    assert cwv.cwv_lcp(good, _ctx()).verdict == "skip"
