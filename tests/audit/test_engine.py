"""Audit engine end-to-end: fetch → checks → score → persist (04 §3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.connectors.base import ConnectorAuthError
from kvseo.connectors.gsc import GscPagePull
from kvseo.connectors.psi import PsiConnector
from kvseo.core.audit.checks import REGISTRY
from kvseo.core.audit.engine import SourcePull, run_audit
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


async def test_gsc_pull_runs_beside_psi_and_targets_the_final_url(tmp_path: Path) -> None:
    """02 §7 / 04 §3: the GSC pull and PSI are concurrent connector stages. The
    pull waits until PSI has started, so a serial engine would deadlock it —
    the timeout turns that into a failure instead of a hang."""
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    psi_started = asyncio.Event()
    asked: list[str] = []

    class _Psi:
        async def core_web_vitals(self, url: str) -> None:
            psi_started.set()
            await gsc_done.wait()

    gsc_done = asyncio.Event()

    async def gsc(url: str) -> GscPagePull:
        await psi_started.wait()
        asked.append(url)
        gsc_done.set()
        return GscPagePull(property="https://example.com/", queries=7)

    result = await asyncio.wait_for(
        run_audit(URL, db_engine=engine, fetch_client=_fetch_client(), psi=_Psi(), gsc=gsc),  # type: ignore[arg-type]
        timeout=5,
    )
    assert result.status == "complete"
    assert result.gsc == GscPagePull(property="https://example.com/", queries=7)
    assert result.gsc_error is None
    assert asked == [URL]


async def test_gsc_failure_costs_the_audit_nothing_but_gsc(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)

    async def gsc(url: str) -> GscPagePull:
        raise ConnectorAuthError("Search Console access was revoked")

    result = await run_audit(URL, db_engine=engine, fetch_client=_fetch_client(), psi=None, gsc=gsc)
    assert result.status == "complete"
    assert result.score == 100
    assert result.gsc is None
    assert result.gsc_error == "Search Console access was revoked"


async def test_no_gsc_pull_leaves_both_fields_empty(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    result = await run_audit(URL, db_engine=get_engine(db), fetch_client=_fetch_client(), psi=None)
    assert (result.gsc, result.gsc_error) == (None, None)


async def test_source_pulls_run_and_fail_independently(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    asked: list[str] = []

    async def serpbear(url: str) -> SourcePull:
        asked.append(url)
        return SourcePull(source="serpbear", rows=3, detail="3 tracked keywords for example.com")

    async def dataforseo(url: str) -> SourcePull:
        raise ConnectorAuthError("DataForSEO 40200: Insufficient funds.")

    async def openseo(url: str) -> SourcePull:
        # An upstream reply in a shape the parser never saw: not a ConnectorError.
        raise KeyError("structuredContent")

    result = await run_audit(
        URL, db_engine=get_engine(db), fetch_client=_fetch_client(), psi=None,
        sources={"serpbear": serpbear, "dataforseo": dataforseo, "openseo": openseo},
    )
    assert result.status == "complete"
    assert [p.source for p in result.sources] == ["serpbear"]
    assert result.source_errors == {
        "dataforseo": "DataForSEO 40200: Insufficient funds.",
        "openseo": "KeyError: 'structuredContent'",
    }
    assert asked == [URL]
