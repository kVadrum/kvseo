"""SQLAlchemy models — the schema source of truth (07-data-model.md §3).

Conventions (data-model §1): UUID primary keys stored as 16 raw bytes
(``UuidBytes``); timestamps are TEXT columns defaulting to ``datetime('now')``
(UTC); type-flexible columns use ``JSON``; status/verdict columns carry CHECK
constraints. This module deliberately omits ``from __future__ import
annotations`` — SQLAlchemy resolves ``Mapped[...]`` annotations at mapping time.

v0.1 tables only. The SerpBear / OpenSEO / DataForSEO connector tables (§3.7)
land with those connectors in v0.2, alongside their own migration.
"""

import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from kvseo.storage.types import UuidBytes

_NOW = "(datetime('now'))"  # SQLite server-side UTC timestamp default


class Base(DeclarativeBase):
    """Declarative base for all kvseo models."""


class Client(Base):
    """An agency client. First-class in v0.3; present in v0.1 only as the FK
    target for ``sites.client_id`` (data-model §3.4)."""

    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))
    updated_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text(_NOW), onupdate=func.datetime("now")
    )


class Site(Base):
    """A site (origin) under management (data-model §3.3)."""

    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    origin: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidBytes(), ForeignKey("clients.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))
    updated_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text(_NOW), onupdate=func.datetime("now")
    )


class AuditRun(Base):
    """One audit of one URL — immutable history (data-model §3.1)."""

    __tablename__ = "audit_runs"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_url: Mapped[str | None] = mapped_column(Text)
    keyword: Mapped[str | None] = mapped_column(String)
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidBytes(), ForeignKey("sites.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'running'"))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    page_title: Mapped[str | None] = mapped_column(Text)
    page_status_code: Mapped[int | None] = mapped_column(Integer)
    fetch_duration_ms: Mapped[int | None] = mapped_column(Integer)
    score: Mapped[int | None] = mapped_column(Integer)  # 0-100, NULL if failed
    strategy: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'mobile'"))
    fetched_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))
    completed_at: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'complete', 'failed')", name="ck_audit_runs_status"
        ),
        # Plain composite indexes — SQLite scans them backwards for the
        # `ORDER BY ... DESC` queries the CLI runs, so the DESC qualifier in
        # the spec DDL is an omittable optimization (keeps migrations clean).
        Index("idx_audit_runs_url_time", "url", "fetched_at"),
        Index("idx_audit_runs_site_time", "site_id", "fetched_at"),
        Index("idx_audit_runs_status", "status", sqlite_where=text("status != 'complete'")),
    )


class AuditCheck(Base):
    """One on-page check result within an audit run (data-model §3.1)."""

    __tablename__ = "audit_checks"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    audit_run_id: Mapped[uuid.UUID] = mapped_column(
        UuidBytes(), ForeignKey("audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    check_id: Mapped[str] = mapped_column(String, nullable=False)  # e.g. 'title.length'
    verdict: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # per-check JSON payload
    message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))

    __table_args__ = (
        CheckConstraint(
            "verdict IN ('pass', 'warn', 'fail', 'skip', 'error')",
            name="ck_audit_checks_verdict",
        ),
        CheckConstraint(
            "severity IN ('info', 'warn', 'fail')", name="ck_audit_checks_severity"
        ),
        Index("idx_audit_checks_run", "audit_run_id"),
        Index("idx_audit_checks_check", "check_id", "verdict"),
    )


class AdvisorOutput(Base):
    """A single advisor (LLM) call's result, auditable to its source (§3.2)."""

    __tablename__ = "advisor_outputs"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    audit_run_id: Mapped[uuid.UUID] = mapped_column(
        UuidBytes(), ForeignKey("audit_runs.id", ondelete="CASCADE"), nullable=False
    )
    prompt_id: Mapped[str] = mapped_column(String, nullable=False)  # 'prioritize'|'report'|'brief'
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # validated against the schema
    raw_response: Mapped[str | None] = mapped_column(Text)  # unparsed, for debugging
    error: Mapped[str | None] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))

    __table_args__ = (
        CheckConstraint(
            "status IN ('success', 'invalid_output', 'failed')",
            name="ck_advisor_outputs_status",
        ),
        Index("idx_advisor_outputs_audit", "audit_run_id", "prompt_id"),
        Index("idx_advisor_outputs_cost", "created_at"),
    )


class GscQuery(Base):
    """A Google Search Console query row for a page (data-model §3.5)."""

    __tablename__ = "gsc_queries"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    site_origin: Mapped[str] = mapped_column(String, nullable=False)  # denormalized
    page: Mapped[str] = mapped_column(Text, nullable=False)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False)
    ctr: Mapped[float] = mapped_column(Float, nullable=False)
    position: Mapped[float] = mapped_column(Float, nullable=False)
    range_start: Mapped[str] = mapped_column(String, nullable=False)  # ISO date
    range_end: Mapped[str] = mapped_column(String, nullable=False)
    fetched_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))

    __table_args__ = (
        Index("idx_gsc_page_query", "page", "query", "range_end"),
        Index("idx_gsc_site_time", "site_origin", "fetched_at"),
    )


class PsiResult(Base):
    """A PageSpeed Insights result (field + lab data) for a URL (§3.6)."""

    __tablename__ = "psi_results"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[str] = mapped_column(String, nullable=False)
    field_lcp_ms: Mapped[int | None] = mapped_column(Integer)
    field_inp_ms: Mapped[int | None] = mapped_column(Integer)
    field_cls: Mapped[float | None] = mapped_column(Float)
    field_origin_fallback: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("0")
    )
    lab_lcp_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    lab_tbt_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    lab_cls: Mapped[float] = mapped_column(Float, nullable=False)
    lab_performance_score: Mapped[int] = mapped_column(Integer, nullable=False)
    opportunities: Mapped[list[Any] | None] = mapped_column(JSON)
    raw_response: Mapped[str | None] = mapped_column(Text)  # compressed full PSI JSON
    fetched_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))

    __table_args__ = (Index("idx_psi_url_time", "url", "strategy", "fetched_at"),)


class Report(Base):
    """A generated client report artifact (data-model §3.8)."""

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(UuidBytes(), primary_key=True, default=uuid.uuid4)
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidBytes(), ForeignKey("sites.id", ondelete="SET NULL")
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidBytes(), ForeignKey("clients.id", ondelete="SET NULL")
    )
    range_start: Mapped[str] = mapped_column(String, nullable=False)
    range_end: Mapped[str] = mapped_column(String, nullable=False)
    template: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'monthly'"))
    format: Mapped[str] = mapped_column(String, nullable=False)  # 'pdf'|'docx'|'md'|'html'
    file_path: Mapped[str | None] = mapped_column(Text)
    narrative_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidBytes(), ForeignKey("advisor_outputs.id", ondelete="SET NULL")
    )
    created_at: Mapped[str] = mapped_column(String, nullable=False, server_default=text(_NOW))

    __table_args__ = (Index("idx_reports_site_time", "site_id", "range_end"),)


class SchemaMeta(Base):
    """Schema/bookkeeping key-values (data-model §3.9): schema version, last
    vacuum, last litestream snapshot, install id. Alembic owns the migration
    version separately in its own ``alembic_version`` table."""

    __tablename__ = "schema_meta"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text(_NOW), onupdate=func.datetime("now")
    )
