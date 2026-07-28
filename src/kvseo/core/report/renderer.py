"""Report renderer: one stored audit -> a self-contained report (ADR-005).

v0.1 renders a *single* audit run (plus its latest advisor recommendation and
Core Web Vitals) to either a self-contained HTML file — inline CSS, system fonts,
zero external requests, print-to-PDF friendly — or clean Markdown. The monthly
aggregate report (``kvseo report`` over a date range) needs month-over-month
history to be useful and lands with that history in a later cut (05 §1).

The renderer is read-only: it loads from SQLite and returns a string. Writing
the file and recording the ``reports`` row is the CLI's job, keeping this
function pure and easy to snapshot-test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jinja2
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.gsc import GscQueryRow, recent_queries
from kvseo.core.advisor.client import latest_run
from kvseo.storage.models import AuditCheck as AuditCheckORM
from kvseo.storage.models import AuditRun as AuditRunORM
from kvseo.storage.models import PsiResult as PsiResultORM

_TEMPLATES = Path(__file__).resolve().parent / "templates"

# Google's Core Web Vitals rating thresholds: (good_max, poor_min).
_CWV_BANDS: dict[str, tuple[float, float]] = {
    "lcp": (2500, 4000),  # ms
    "inp": (200, 500),  # ms
    "cls": (0.1, 0.25),  # unitless
}

# Search Console: how many queries the report shows, and what counts as a
# "striking distance" opportunity -- a query with real demand sitting at
# page-1-bottom / page-2 (positions 5-20), where a small rank gain unlocks
# outsized clicks. This is the single most actionable slice of GSC data.
#
# The impressions floor keeps long-tail noise out: a query at position 12 with
# 3 impressions is a rounding error, not an opportunity worth an action item.
_GSC_LIMIT = 15
_STRIKING_POS = (5.0, 20.0)
_STRIKING_MIN_IMPRESSIONS = 10


class ReportError(Exception):
    """The report could not be rendered (audit missing, or bad format)."""


def render(audit_id: uuid.UUID, fmt: str = "html", *, engine: Engine) -> str:
    """Render a stored audit to a report string in ``fmt`` ('html' or 'md')."""
    if fmt not in ("html", "md"):
        raise ReportError(f"unsupported format '{fmt}' — v0.1 renders 'html' or 'md'")
    data = _load(audit_id, engine)
    template = _environment().get_template(f"report.{fmt}.j2")
    return template.render(**data)


def _environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES),
        autoescape=jinja2.select_autoescape(["html.j2", "html"]),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=jinja2.StrictUndefined,
    )
    env.filters["paragraphs"] = _paragraphs
    return env


def _load(audit_id: uuid.UUID, engine: Engine) -> dict[str, Any]:
    with Session(engine) as session:
        run = session.get(AuditRunORM, audit_id)
        if run is None:
            raise ReportError(f"audit {audit_id} not found")
        checks = session.scalars(
            select(AuditCheckORM)
            .where(AuditCheckORM.audit_run_id == audit_id)
            .order_by(AuditCheckORM.check_id)
        ).all()
        psi = (
            session.scalars(
                select(PsiResultORM)
                .where(PsiResultORM.url == run.fetched_url)
                .order_by(PsiResultORM.fetched_at.desc())
                .limit(1)
            ).first()
            if run.fetched_url
            else None
        )
        gsc_rows = (
            recent_queries(session, run.fetched_url, limit=_GSC_LIMIT) if run.fetched_url else []
        )
    advisor = latest_run(audit_id, engine)
    gsc = _gsc(gsc_rows)

    failed = [_check(c) for c in checks if c.verdict == "fail"]
    warned = [_check(c) for c in checks if c.verdict == "warn"]
    passed = [c.check_id for c in checks if c.verdict == "pass"]
    skipped = [c.check_id for c in checks if c.verdict in ("skip", "error")]

    return {
        "url": run.url,
        "fetched_url": run.fetched_url,
        "page_title": run.page_title,
        "status": run.status,
        "score": run.score,
        "score_band": _score_band(run.score),
        "run_id": str(audit_id),
        "keyword": run.keyword,
        "fetched_at": run.fetched_at,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "counts": {
            "pass": len(passed),
            "warn": len(warned),
            "fail": len(failed),
            "skip": len(skipped),
        },
        "failed": failed,
        "warned": warned,
        "passed": passed,
        "skipped": skipped,
        "advisor": advisor.output if advisor and advisor.status == "success" else None,
        "advisor_status": advisor.status if advisor else None,
        "advisor_cost": advisor.estimated_cost_usd if advisor else None,
        "cwv": _cwv(psi),
        "lab_performance": psi.lab_performance_score if psi else None,
        "gsc": gsc,
        "gsc_striking": sum(1 for q in gsc if q["striking"]),
        "gsc_range": _gsc_range(gsc_rows),
    }


def _check(c: AuditCheckORM) -> dict[str, Any]:
    return {
        "id": c.check_id,
        "severity": c.severity,
        "message": c.message or "",
        "data": dict(c.data or {}),
    }


def _score_band(score: int | None) -> str:
    if score is None:
        return "na"
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 50:
        return "fair"
    return "poor"


def _gsc(rows: list[GscQueryRow]) -> list[dict[str, Any]]:
    """Search Console rows shaped for display, newest batch, highest impressions
    first. ``striking`` marks the actionable slice (see the constants above);
    the templates lean on it for both the row emphasis and the summary count."""
    low, high = _STRIKING_POS
    return [
        {
            "query": r.query,
            "clicks": r.clicks,
            "impressions": r.impressions,
            "ctr": f"{r.ctr * 100:.1f}%",
            "position": f"{r.position:.1f}",
            "striking": (
                low <= r.position <= high and r.impressions >= _STRIKING_MIN_IMPRESSIONS
            ),
        }
        for r in rows
    ]


def _gsc_range(rows: list[GscQueryRow]) -> str | None:
    """The date window the displayed GSC rows cover, for the section subhead.

    Rows come from a single ``fetched_at`` batch but each carries its own
    range, so span the outer bounds rather than trusting the first row."""
    if not rows:
        return None
    start = min(r.date_range_start for r in rows)
    end = max(r.date_range_end for r in rows)
    return f"{start.isoformat()} to {end.isoformat()}"


def _cwv(psi: PsiResultORM | None) -> list[dict[str, Any]]:
    """Three Core Web Vitals rows, preferring real field data, falling back to
    lab where field data is absent. Returns [] when no PSI result was stored."""
    if psi is None:
        return []
    rows = [
        _metric("LCP", "lcp", "ms", psi.field_lcp_ms, psi.lab_lcp_ms),
        _metric("INP", "inp", "ms", psi.field_inp_ms, None),  # no lab INP; lab measures TBT
        _metric("CLS", "cls", "", psi.field_cls, psi.lab_cls),
    ]
    return rows


def _metric(
    label: str, key: str, unit: str, field: float | None, lab: float | None
) -> dict[str, Any]:
    primary = field if field is not None else lab
    return {
        "label": label,
        "unit": unit,
        "field": _fmt_metric(key, field, unit),
        "lab": _fmt_metric(key, lab, unit),
        "source": "field" if field is not None else ("lab" if lab is not None else "none"),
        "rating": _rate(key, primary),
    }


def _fmt_metric(key: str, value: float | None, unit: str) -> str | None:
    if value is None:
        return None
    if key == "cls":
        return f"{value:.2f}"
    return f"{round(value)}{unit}"


def _rate(key: str, value: float | None) -> str:
    if value is None:
        return "na"
    good, poor = _CWV_BANDS[key]
    if value <= good:
        return "good"
    if value > poor:
        return "poor"
    return "ni"  # needs improvement


def _paragraphs(text: str) -> list[str]:
    """Split prose on blank lines into paragraphs (for the template to wrap in
    <p>). Keeps the renderer free of raw-HTML injection — Jinja autoescapes the
    text, so model output can't smuggle markup into the report."""
    return [block.strip() for block in (text or "").split("\n\n") if block.strip()]
