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


# --- Checks added alongside the v0.1 registry completion (04 §2) ------------


def test_headings_keyword(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert headings.keyword(good, _ctx("seo audit")).verdict == "pass"
    assert headings.keyword(good, _ctx()).verdict == "skip"  # no keyword set
    assert headings.keyword(poor, _ctx("seo audit")).verdict == "warn"
    # Any H1 may carry the keyword — a multi-H1 page is already flagged by
    # headings.h1.presence and shouldn't also report a spurious keyword miss.
    assert headings.keyword(poor, _ctx("second heading")).verdict == "pass"


def test_headings_keyword_skips_without_h1() -> None:
    doc = ParsedDocument("<html><body><h2>No h1 here</h2></body></html>", _BASE)
    result = headings.keyword(doc, _ctx("anything"))
    assert result.verdict == "skip"
    assert result.data["reason"] == "no_h1"


def test_images_dimensions(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert content.images_dimensions(good, _ctx()).verdict == "pass"
    poor_result = content.images_dimensions(poor, _ctx())
    assert poor_result.verdict == "warn"
    assert poor_result.data["missing_dimensions"] == ["https://example.com/photo.png"]

    # No images at all is a skip, not a vacuous pass.
    empty = ParsedDocument("<html><body><p>no images</p></body></html>", _BASE)
    assert content.images_dimensions(empty, _ctx()).verdict == "skip"


def test_links_external_rel(good: ParsedDocument) -> None:
    # The good fixture's external link has no target=_blank, so nothing to flag.
    assert content.links_external_rel(good, _ctx()).verdict == "pass"

    offending = ParsedDocument(
        '<html><body>'
        '<a href="https://evil.test/x" target="_blank">no rel</a>'
        '<a href="https://ok.test/y" target="_blank" rel="noopener noreferrer">safe</a>'
        '<a href="/internal" target="_blank">same host</a>'
        "</body></html>",
        _BASE,
    )
    result = content.links_external_rel(offending, _ctx())
    assert result.verdict == "warn"
    # Only the external, _blank, rel-less link — not the safe one, not same-host.
    assert result.data["offenders"] == ["https://evil.test/x"]


def test_schema_valid(good: ParsedDocument, poor: ParsedDocument) -> None:
    assert content.schema_valid(good, _ctx()).verdict == "pass"
    assert content.schema_valid(poor, _ctx()).verdict == "skip"  # no blocks at all


@pytest.mark.parametrize(
    ("block", "error"),
    [
        ("{not json at all", "not valid JSON"),
        ('{"@context":"https://schema.org"}', "no @type"),
        ('{"@type":"Article"}', "@context is not schema.org"),
        ('{"@context":"https://example.org/vocab","@type":"Article"}', "@context is not schema.org"),
    ],
)
def test_schema_valid_flags_bad_blocks(block: str, error: str) -> None:
    doc = ParsedDocument(
        f'<html><head><script type="application/ld+json">{block}</script></head></html>', _BASE
    )
    result = content.schema_valid(doc, _ctx())
    assert result.verdict == "warn"
    assert result.data["errors"] == [{"block": "0", "error": error}]


def test_schema_valid_accepts_http_and_list_context() -> None:
    """http:// schema.org and a list-form @context are both legitimate."""
    doc = ParsedDocument(
        '<html><head>'
        '<script type="application/ld+json">{"@context":"http://schema.org","@type":"Organization"}</script>'
        '<script type="application/ld+json">'
        '{"@context":["https://schema.org","https://example.org/x"],"@type":"Person"}</script>'
        "</head></html>",
        _BASE,
    )
    assert content.schema_valid(doc, _ctx()).verdict == "pass"


def test_internal_links_broken_skips_when_not_requested(good: ParsedDocument) -> None:
    """Unrequested must skip, never pass — 'no broken links' for links nobody
    probed would be a false all-clear."""
    result = content.internal_links_broken(good, _ctx())
    assert result.verdict == "skip"
    assert result.data["reason"] == "not_requested"


def test_internal_links_broken_reports_failures(good: ParsedDocument) -> None:
    ctx = AuditContext(
        fetched_url=_BASE,
        internal_link_status={
            "https://example.com/about": 200,
            "https://example.com/gone": 404,
            "https://example.com/dead": None,  # never completed
            "https://example.com/moved": 301,  # redirects are not broken
        },
    )
    result = content.internal_links_broken(good, ctx)
    assert result.verdict == "warn"
    assert result.data["checked"] == 4
    assert result.data["broken"] == {
        "https://example.com/gone": "404",
        "https://example.com/dead": "unreachable",
    }


def test_internal_links_broken_all_healthy(good: ParsedDocument) -> None:
    ctx = AuditContext(
        fetched_url=_BASE, internal_link_status={"https://example.com/about": 200}
    )
    assert content.internal_links_broken(good, ctx).verdict == "pass"


def test_malformed_href_does_not_raise() -> None:
    """A href stdlib's urljoin rejects ("Invalid IPv6 URL") is skipped, not
    propagated. It used to escape links(), which made one bad anchor able to
    abort whatever was iterating — a single errored check when a check called
    it, but the entire audit once the engine started calling it directly for
    the internal-link probe."""
    doc = ParsedDocument(
        '<html><body>'
        '<a href="http://[bad/y">malformed</a>'
        '<a href="/fine">fine</a>'
        "</body></html>",
        _BASE,
    )
    links = doc.links()  # must not raise
    assert [link.href for link in links] == ["https://example.com/fine"]
    # And the check over it now returns a real verdict instead of "error".
    assert content.internal_links_count(doc, _ctx()).verdict == "warn"


@pytest.mark.parametrize(
    ("block", "expected_types"),
    [
        # Codex /cr finding: the @graph wrapper is what Yoast / RankMath and most
        # WordPress SEO plugins emit. Reading only the top level saw no @type and
        # reported valid structured data as broken.
        (
            '{"@context":"https://schema.org","@graph":'
            '[{"@type":"Organization"},{"@type":"WebSite"}]}',
            ["Organization", "WebSite"],
        ),
        # A single-object @graph, and @type lists, both occur in the wild.
        ('{"@context":"https://schema.org","@graph":{"@type":"Person"}}', ["Person"]),
        (
            '{"@context":"https://schema.org","@type":["Article","BlogPosting"]}',
            ["Article", "BlogPosting"],
        ),
    ],
)
def test_schema_graph_shapes_are_understood(block: str, expected_types: list[str]) -> None:
    doc = ParsedDocument(
        f'<html><head><script type="application/ld+json">{block}</script></head></html>', _BASE
    )
    assert doc.schema_blocks()[0].types == expected_types
    assert content.schema_valid(doc, _ctx()).verdict == "pass"
