"""SQLAlchemy models.

Only the infrastructure ``_kvseo_meta`` table exists at scaffold stage; the
domain tables (``audit_runs``, ``audit_checks``, ``gsc_queries``,
``psi_results``, ``advisor_outputs``, ``reports``) are built against
07-data-model.md. Note: this module deliberately does NOT use
``from __future__ import annotations`` — SQLAlchemy's declarative mapper
resolves ``Mapped[...]`` annotations at class-creation time.
"""

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for all kvseo models."""


class SchemaMeta(Base):
    """Schema/bookkeeping key-values.

    Keeps a freshly-initialised database from being empty and anchors the
    migration story (Alembic wiring is the first storage-build task; ADR-003,
    open question Q9).
    """

    __tablename__ = "_kvseo_meta"

    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]
