"""Audit engine end-to-end: fetch → checks → score → persist (04 §3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.connectors.psi import PsiConnector
from kvseo.core.audit.checks import REGISTRY
from kvseo.core.audit.engine import run_audit
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import AuditCheck as AuditCheckORM
from kvseo.storage.models import AuditRun as AuditRunORM

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_HTML = (_FIXTURES / "html" / "good.html").read_text()
_PSI = json.loads((_FIXTURES / "psi" / "example_mobile.json").read_text())
URL = "https://example.com/"


def _fetch_client(html: str = _HTML, status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=html)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _failing_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _psi_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_PSI)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_run_audit_complete(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    result = await run_audit(URL, db_engine=engine, fetch_client=_fetch_client(), psi=None)

    assert result.status == "complete"
    # Good page; keyword + cwv.* skip and drop out, so the rest are all passes.
    assert result.score == 100
    assert result.page_title == "SEO Audit Checklist for Small Business Sites"
    assert len(result.checks) == len(REGISTRY)

    with Session(engine) as s:
        run = s.scalars(select(AuditRunORM)).one()
        assert run.status == "complete"
        assert run.score == 100
        assert run.completed_at is not None
        assert s.scalar(select(func.count()).select_from(AuditCheckORM)) == len(REGISTRY)


async def test_run_audit_integrates_cwv(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    psi = PsiConnector(engine=engine, client=_psi_client())
    result = await run_audit(URL, db_engine=engine, fetch_client=_fetch_client(), psi=psi)

    by_id = {c.check_id: c for c in result.checks}
    assert by_id["cwv.lcp"].verdict == "pass"  # fixture field LCP 2100 < 2500
    assert by_id["cwv.lcp"].data["source"] == "field"
    assert by_id["cwv.cls"].verdict == "pass"


async def test_run_audit_http_error_marks_failed(tmp_path: Path) -> None:
    # A 404/500 page is an HTTP error (spec 04 §3): the audit must fail and
    # abort, never score the error page or write check rows.
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    result = await run_audit(URL, db_engine=engine, fetch_client=_fetch_client(status=404), psi=None)

    assert result.status == "failed"
    assert result.failure_reason == "http_404"
    assert result.score is None
    assert result.page_status_code == 404
    assert result.checks == []

    with Session(engine) as s:
        run = s.scalars(select(AuditRunORM)).one()
        assert run.status == "failed"
        assert run.failure_reason == "http_404"
        assert run.page_status_code == 404
        assert run.score is None
        assert s.scalar(select(func.count()).select_from(AuditCheckORM)) == 0


async def test_run_audit_fetch_failure_marks_failed(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    result = await run_audit(URL, db_engine=engine, fetch_client=_failing_client(), psi=None)

    assert result.status == "failed"
    assert result.failure_reason is not None
    assert result.score is None
    with Session(engine) as s:
        run = s.scalars(select(AuditRunORM)).one()
        assert run.status == "failed"
        assert run.failure_reason is not None
