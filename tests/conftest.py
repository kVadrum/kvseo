"""Shared test helpers: seed a completed audit (+ optional GSC/PSI) into a temp DB.

The advisor-context, advisor-client, and report-renderer tests all need a
realistic stored audit to read back, so the seeding logic lives here once.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import AuditCheck, AuditRun, GscQuery, PsiResult

_TS = "%Y-%m-%d %H:%M:%S"
URL = "https://kemek.net/services"


def _now() -> str:
    return datetime.now(UTC).strftime(_TS)


def seed_audit(
    engine: Engine,
    *,
    status: str = "complete",
    with_gsc: bool = True,
    with_psi: bool = True,
) -> uuid.UUID:
    """Insert one audit run with a representative spread of check verdicts."""
    audit_id = uuid.uuid4()
    with Session(engine) as s:
        s.add(
            AuditRun(
                id=audit_id,
                url=URL,
                fetched_url=URL,
                keyword="ops consulting",
                status=status,
                page_title="Operations Consulting — KeMeK",
                page_status_code=200,
                fetch_duration_ms=180,
                score=72 if status == "complete" else None,
                completed_at=_now() if status == "complete" else None,
            )
        )
        # Flush the parent before the children: there's no ORM relationship()
        # between AuditRun and AuditCheck (only a raw FK column), so the unit of
        # work won't order the inserts on its own, and foreign_keys=ON is enforced.
        s.flush()
        checks = [
            ("title.length", "warn", "warn", {"length": 71}, "Title 71 chars exceeds 60"),
            ("meta.canonical", "fail", "fail", {"present": False}, "No canonical tag"),
            ("headings.h1", "pass", "info", {"count": 1}, "Exactly one <h1>"),
            ("content.wordcount", "pass", "info", {"words": 820}, "820 words"),
            ("cwv.lcp", "skip", "info", {}, "CWV unavailable"),
        ]
        for cid, verdict, severity, data, msg in checks:
            s.add(
                AuditCheck(
                    audit_run_id=audit_id,
                    check_id=cid,
                    verdict=verdict,
                    severity=severity,
                    data=data or None,
                    message=msg,
                )
            )
        if with_gsc:
            for q, impr, clicks, ctr, pos in [
                ("ops consulting west virginia", 412, 8, 0.019, 14.2),
                ("kemek network", 188, 41, 0.218, 1.2),
                ("ops consulting", 96, 3, 0.031, 8.7),
            ]:
                s.add(
                    GscQuery(
                        site_origin="https://kemek.net/",
                        page=URL,
                        query=q,
                        clicks=clicks,
                        impressions=impr,
                        ctr=ctr,
                        position=pos,
                        range_start="2026-05-01",
                        range_end="2026-05-31",
                        fetched_at=_now(),
                    )
                )
        if with_psi:
            s.add(
                PsiResult(
                    url=URL,
                    strategy="mobile",
                    field_lcp_ms=3120,
                    field_inp_ms=180,
                    field_cls=0.08,
                    field_origin_fallback=False,
                    lab_lcp_ms=2980,
                    lab_tbt_ms=210,
                    lab_cls=0.05,
                    lab_performance_score=68,
                    opportunities=[
                        {
                            "id": "unused-css-rules",
                            "title": "Reduce unused CSS",
                            "description": "…",
                            "savings_ms": 450,
                            "score": 0.4,
                        }
                    ],
                    fetched_at=_now(),
                )
            )
        s.commit()
    return audit_id


@pytest.fixture
def audit_engine(tmp_path: Path) -> Engine:
    db = tmp_path / "kvseo.db"
    migrate(db)
    return get_engine(db)


@pytest.fixture
def seed() -> Callable[..., uuid.UUID]:
    return seed_audit
