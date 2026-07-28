"""Context assembly for the advisor (05-advisor-prompts.md §3).

The advisor never touches SQLite. It sees a :class:`Context` assembled here from
the stored audit plus the connector rows that audit can reference. Two payoffs
(05 §3): runs are reproducible from the context bytes, and every recommendation
the model makes can be traced back to a context row (R1).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.gsc import GscQueryRow, recent_queries
from kvseo.connectors.psi import PsiConnector, PsiResult
from kvseo.storage.models import AuditCheck as AuditCheckORM
from kvseo.storage.models import AuditRun as AuditRunORM
from kvseo.storage.models import PsiResult as PsiResultORM
from kvseo.storage.timestamps import parse_ts

DEFAULT_MAX_GSC_QUERIES = 25


class AdvisorError(Exception):
    """The advisor cannot run on this audit (missing, incomplete, or oversized)."""


class CheckSummary(BaseModel):
    """One check's outcome, compacted for the model."""

    id: str
    severity: str
    message: str
    data: dict[str, object] = Field(default_factory=dict)


class AuditSummary(BaseModel):
    """The audit, bucketed by verdict. Failed/warned checks ship in full; passed
    and not-assessed checks ship as bare ID lists to save tokens (05 §3)."""

    url: str
    fetched_url: str | None
    score: int | None
    page_title: str | None
    failed: list[CheckSummary] = Field(default_factory=list)
    warned: list[CheckSummary] = Field(default_factory=list)
    passed_ids: list[str] = Field(default_factory=list)
    not_assessed_ids: list[str] = Field(default_factory=list)


class Context(BaseModel):
    """Everything — and only — the advisor sees for one audit."""

    audit: AuditSummary
    gsc_queries: list[GscQueryRow] = Field(default_factory=list)
    psi: PsiResult | None = None
    target_keyword: str | None = None


def build_context(
    audit_id: uuid.UUID, engine: Engine, *, max_gsc_queries: int = DEFAULT_MAX_GSC_QUERIES
) -> Context:
    """Assemble the advisor context for a completed audit.

    Raises :class:`AdvisorError` if the audit is missing or did not complete —
    there's nothing to advise on a failed fetch.
    """
    with Session(engine) as session:
        run = session.get(AuditRunORM, audit_id)
        if run is None:
            raise AdvisorError(f"audit {audit_id} not found")
        if run.status != "complete":
            raise AdvisorError(
                f"audit {audit_id} is '{run.status}', not 'complete' — nothing to advise on"
            )
        checks = session.scalars(
            select(AuditCheckORM).where(AuditCheckORM.audit_run_id == audit_id)
        ).all()
        audit = _summarize(run, checks)
        gsc = recent_queries(session, run.fetched_url, limit=max_gsc_queries) if run.fetched_url else []
        psi = _recent_psi(session, run.fetched_url) if run.fetched_url else None

    return Context(audit=audit, gsc_queries=gsc, psi=psi, target_keyword=run.keyword)


def _summarize(run: AuditRunORM, checks: Sequence[AuditCheckORM]) -> AuditSummary:
    failed, warned, passed, other = [], [], [], []
    for c in checks:
        if c.verdict == "fail":
            failed.append(_check(c))
        elif c.verdict == "warn":
            warned.append(_check(c))
        elif c.verdict == "pass":
            passed.append(c.check_id)
        else:  # 'skip' or 'error' — recorded but not a usable signal
            other.append(c.check_id)
    return AuditSummary(
        url=run.url,
        fetched_url=run.fetched_url,
        score=run.score,
        page_title=run.page_title,
        failed=failed,
        warned=warned,
        passed_ids=passed,
        not_assessed_ids=other,
    )


def _check(c: AuditCheckORM) -> CheckSummary:
    return CheckSummary(
        id=c.check_id, severity=c.severity, message=c.message or "", data=dict(c.data or {})
    )


def _recent_psi(session: Session, url: str) -> PsiResult | None:
    row = session.scalars(
        select(PsiResultORM)
        .where(PsiResultORM.url == url)
        .order_by(PsiResultORM.fetched_at.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    # Same ORM→PsiResult mapping the PSI connector already owns — reuse it so the
    # 12-field shape lives in one place, not two.
    return PsiConnector._orm_to_result(row, parse_ts(row.fetched_at))
