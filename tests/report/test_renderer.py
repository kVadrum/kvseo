"""Report renderer: HTML + Markdown output from a stored audit (+ advisor row)."""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.core.advisor.schemas import PrioritizationOutput, PriorityAction
from kvseo.core.report.renderer import ReportError, render
from kvseo.storage.models import AdvisorOutput as AdvisorOutputORM


def _add_advisor(engine: Engine, audit_id: uuid.UUID) -> None:
    output = PrioritizationOutput(
        summary="The page competes for 'ops consulting' at position 14.",
        actions=[
            PriorityAction(
                rank=1,
                title="Rewrite the title tag around 'ops consulting'",
                description="Lead with the keyword; trim to 55 chars.",
                rationale="412 impressions, 1.9% CTR at position 14.",
                expected_impact="high",
                effort="low",
                evidence=["title.length", "gsc.queries[0]"],
                category="on_page",
            )
        ],
        things_going_well=["Brand query at position 1.2 with 21.8% CTR."],
        cautions=["CWV field data is origin-level, not URL-level."],
    )
    with Session(engine) as s:
        s.add(
            AdvisorOutputORM(
                audit_run_id=audit_id,
                prompt_id="prioritize",
                provider="anthropic",
                model="claude-haiku-4-5",
                status="success",
                output=output.model_dump(mode="json"),
                estimated_cost_usd=0.0011,
            )
        )
        s.commit()


def test_html_is_self_contained_and_complete(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    _add_advisor(audit_engine, aid)
    html = render(aid, "html", engine=audit_engine)

    assert html.startswith("<!doctype html>")
    # Self-contained: no external resource references.
    for needle in ("http://", "https://fonts", "src=", "<link", "<script"):
        assert needle not in html.replace("https://kemek.net", "")  # the audited URL is allowed
    # Content present.
    assert "https://kemek.net/services" in html
    assert ">72<" in html  # the score
    assert "meta.canonical" in html  # the failure
    assert "title.length" in html  # the warning
    assert "Rewrite the title tag" in html  # advisor action
    assert "Evidence" in html and "gsc.queries[0]" in html
    assert "LCP" in html and "3120ms" in html  # CWV field data
    assert "print to PDF" in html


def test_html_without_advisor_shows_prompt(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    html = render(aid, "html", engine=audit_engine)
    assert "No AI recommendations" in html
    assert f"kvseo advisor run {aid}" in html


def test_markdown_renders(audit_engine: Engine, seed: Callable[..., uuid.UUID]) -> None:
    aid = seed(audit_engine)
    _add_advisor(audit_engine, aid)
    md = render(aid, "md", engine=audit_engine)

    assert md.startswith("# kvseo audit — https://kemek.net/services")
    assert "## Site health: 72/100" in md
    assert "### 1. Rewrite the title tag" in md
    assert "`title.length`" in md
    assert "Core Web Vitals" in md
    # Markdown must not carry HTML tags from the template.
    assert "<div" not in md


def test_unsupported_format_raises(audit_engine: Engine, seed: Callable[..., uuid.UUID]) -> None:
    aid = seed(audit_engine)
    with pytest.raises(ReportError, match="unsupported format"):
        render(aid, "pdf", engine=audit_engine)


def test_missing_audit_raises(audit_engine: Engine) -> None:
    with pytest.raises(ReportError, match="not found"):
        render(uuid.uuid4(), "html", engine=audit_engine)
