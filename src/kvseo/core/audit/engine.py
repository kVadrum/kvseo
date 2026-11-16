"""Audit engine orchestration (04-audit-engine.md §3).

Deterministic core — no LLM calls. Creates the audit_run row, fetches once,
parses, then runs the network stages concurrently (PSI for CWV, the GSC pull,
the opt-in internal-link probe), runs every registered check against the parsed
document, scores, and persists. The advisor (separate module) runs against the
stored result afterwards, reading the GSC rows this pull persisted.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.base import ConnectorError
from kvseo.connectors.gsc import GscPagePull
from kvseo.connectors.psi import PsiConnector, PsiResult
from kvseo.core.audit.checks import REGISTRY, AuditContext, CheckFn, CheckResult
from kvseo.core.audit.document import ParsedDocument, is_internal
from kvseo.core.audit.fetcher import FetchError, fetch
from kvseo.core.audit.link_checker import LinkStatus, probe_links
from kvseo.core.audit.scoring import score
from kvseo.storage.models import AuditCheck as AuditCheckORM
from kvseo.storage.models import AuditRun as AuditRunORM
from kvseo.storage.timestamps import now_str


class CheckOutcome(BaseModel):
    check_id: str
    verdict: str
    severity: str
    data: dict[str, Any] = Field(default_factory=dict)
    message: str


class AuditResult(BaseModel):
    """The §4.1 audit_run shape plus its per-check outcomes."""

    id: uuid.UUID
    url: str
    fetched_url: str | None
    status: str
    failure_reason: str | None
    keyword: str | None
    score: int | None
    page_title: str | None
    page_status_code: int | None
    fetch_duration_ms: int | None
    checks: list[CheckOutcome] = Field(default_factory=list)
    # None when no pull was attempted (GSC not connected, or --no-gsc).
    gsc: GscPagePull | None = None
    gsc_error: str | None = None


async def run_audit(
    url: str,
    *,
    db_engine: Engine,
    keyword: str | None = None,
    psi: PsiConnector | None = None,
    fetch_client: httpx.AsyncClient | None = None,
    check_internal_links: bool = False,
    gsc: Callable[[str], Awaitable[GscPagePull]] | None = None,
) -> AuditResult:
    """Audit ``url``. ``gsc`` pulls the page's Search Console queries given the
    post-redirect URL; it runs beside PSI, and its failure costs the audit
    nothing but the GSC context (02-architecture.md §10)."""
    audit_id = uuid.uuid4()
    _insert_running(db_engine, audit_id, url, keyword)

    try:
        fetched = await fetch(url, client=fetch_client)
    except FetchError as exc:
        _mark_failed(db_engine, audit_id, exc.reason)
        return AuditResult(
            id=audit_id, url=url, fetched_url=None, status="failed",
            failure_reason=exc.reason, keyword=keyword, score=None,
            page_title=None, page_status_code=None, fetch_duration_ms=None,
        )

    # Spec 04 §3: a non-2xx response is an HTTP error — fail and abort before
    # scoring. Otherwise the audit reports on-page scores for a 404/500 error
    # page as if it were the real page (a 404 is itself the worst SEO outcome).
    if fetched.status_code >= 400:
        reason = f"http_{fetched.status_code}"
        _mark_failed(
            db_engine, audit_id, reason,
            fetched_url=fetched.final_url, status_code=fetched.status_code,
            duration_ms=fetched.duration_ms,
        )
        return AuditResult(
            id=audit_id, url=url, fetched_url=fetched.final_url, status="failed",
            failure_reason=reason, keyword=keyword, score=None,
            page_title=None, page_status_code=fetched.status_code,
            fetch_duration_ms=fetched.duration_ms,
        )

    doc = ParsedDocument(fetched.html, fetched.final_url)

    # Every stage below targets the post-redirect URL so CWV, GSC and the
    # on-page checks all describe the same page.
    psi_result, (gsc_pull, gsc_error), link_status = await asyncio.gather(
        _cwv(psi, fetched.final_url),
        _gsc(gsc, fetched.final_url),
        _probe_internal(doc, fetched.final_url) if check_internal_links else _none(),
    )

    ctx = AuditContext(
        fetched_url=fetched.final_url,
        keyword=keyword,
        psi_result=psi_result,
        internal_link_status=link_status,
    )
    results = [_run_check(check, doc, ctx) for check in REGISTRY]
    audit_score = score(results)
    title = doc.title()

    _complete(db_engine, audit_id, fetched.final_url, fetched.status_code,
              fetched.duration_ms, title, audit_score, results)

    return AuditResult(
        id=audit_id, url=url, fetched_url=fetched.final_url, status="complete",
        failure_reason=None, keyword=keyword, score=audit_score, page_title=title,
        page_status_code=fetched.status_code, fetch_duration_ms=fetched.duration_ms,
        checks=[
            CheckOutcome(
                check_id=r.check_id, verdict=r.verdict, severity=r.severity,
                data=r.data, message=r.message,
            )
            for r in results
        ],
        gsc=gsc_pull,
        gsc_error=gsc_error,
    )


async def _cwv(psi: PsiConnector | None, url: str) -> PsiResult | None:
    if psi is None:
        return None
    try:
        return await psi.core_web_vitals(url)
    except ConnectorError:
        return None  # cwv.* checks skip; the score covers the rest


async def _gsc(
    gsc: Callable[[str], Awaitable[GscPagePull]] | None, url: str
) -> tuple[GscPagePull | None, str | None]:
    if gsc is None:
        return None, None
    try:
        return await gsc(url), None
    except ConnectorError as exc:
        return None, str(exc)


async def _probe_internal(doc: ParsedDocument, url: str) -> LinkStatus:
    # Off by default (04 §9): up to MAX_LINKS HEAD requests against the audited
    # host is the slowest non-PSI stage and the most likely thing to make a
    # first audit feel broken.
    host = urlparse(url).netloc
    return await probe_links([link.href for link in doc.links() if is_internal(link.href, host)])


async def _none() -> None:
    # None (not {}) when the probe is unrequested, so internal_links.broken skips.
    return None


def _run_check(check: CheckFn, doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    # A buggy check (e.g. on malformed HTML) must not abort the whole audit.
    try:
        return check(doc, ctx)
    except Exception as exc:
        name = getattr(check, "__name__", "unknown")
        return CheckResult(name, "error", "info", {"error": str(exc)}, f"Check raised: {exc}")


def _insert_running(engine: Engine, audit_id: uuid.UUID, url: str, keyword: str | None) -> None:
    with Session(engine) as session:
        session.add(AuditRunORM(id=audit_id, url=url, keyword=keyword, status="running"))
        session.commit()


def _mark_failed(
    engine: Engine,
    audit_id: uuid.UUID,
    reason: str,
    *,
    fetched_url: str | None = None,
    status_code: int | None = None,
    duration_ms: int | None = None,
) -> None:
    # The transport-failure path has no response (all None); the HTTP-error
    # path passes what it got so the stored run records the 404/500.
    with Session(engine) as session:
        run = session.get(AuditRunORM, audit_id)
        if run is not None:
            run.status = "failed"
            run.failure_reason = reason
            if fetched_url is not None:
                run.fetched_url = fetched_url
            if status_code is not None:
                run.page_status_code = status_code
            if duration_ms is not None:
                run.fetch_duration_ms = duration_ms
            run.completed_at = now_str()
            session.commit()


def _complete(
    engine: Engine,
    audit_id: uuid.UUID,
    fetched_url: str,
    status_code: int,
    duration_ms: int,
    title: str | None,
    audit_score: int,
    results: list[CheckResult],
) -> None:
    with Session(engine) as session:
        run = session.get(AuditRunORM, audit_id)
        if run is not None:
            run.status = "complete"
            run.fetched_url = fetched_url
            run.page_title = title
            run.page_status_code = status_code
            run.fetch_duration_ms = duration_ms
            run.score = audit_score
            run.completed_at = now_str()
        session.add_all(
            AuditCheckORM(
                audit_run_id=audit_id,
                check_id=r.check_id,
                verdict=r.verdict,
                severity=r.severity,
                data=r.data or None,
                message=r.message,
            )
            for r in results
        )
        session.commit()
