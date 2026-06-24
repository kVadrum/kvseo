"""``kvseo report [AUDIT_ID]`` — render a stored audit to a report file.

v0.1 renders a single audit run (the most recent, or one named by ID) to a
self-contained HTML file or Markdown. The audit's stored advisor recommendation
and Core Web Vitals are folded in. The monthly aggregate report (over a date
range, with a trend narrative) needs accumulated history and lands later (05 §1).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.config import paths
from kvseo.core.report.renderer import ReportError, render
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import AuditRun as AuditRunORM
from kvseo.storage.models import Report as ReportORM

_EXT = {"html": "html", "md": "md"}


def report(
    audit_id: Annotated[
        str | None,
        typer.Argument(help="Audit run ID to render. Defaults to the most recent audit."),
    ] = None,
    report_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Report format: html | md."),
    ] = "html",
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write the report (default: ./)."),
    ] = None,
) -> None:
    """Render a self-contained report from stored audit data."""
    if report_format not in _EXT:
        _fail(f"unsupported --format '{report_format}' (v0.1: html | md).", code=2)

    db = paths.db_path()
    migrate(db)
    engine = get_engine(db)

    aid = _resolve_audit(engine, audit_id)
    try:
        rendered = render(aid, report_format, engine=engine)
    except ReportError as exc:
        _fail(str(exc), code=6)

    path = _write(rendered, output, aid, report_format)
    _record_report(engine, aid, report_format, path)
    typer.echo(f"Wrote {report_format.upper()} report → {path}")


def _resolve_audit(engine: Engine, audit_id: str | None) -> uuid.UUID:
    if audit_id is not None:
        try:
            return uuid.UUID(audit_id)
        except ValueError:
            _fail(f"'{audit_id}' is not a valid audit ID.", code=2)
    # Default to the most recent completed audit.
    with Session(engine) as session:
        row = session.scalars(
            select(AuditRunORM)
            .where(AuditRunORM.status == "complete")
            .order_by(AuditRunORM.created_at.desc())
            .limit(1)
        ).first()
    if row is None:
        _fail("no completed audits to report on — run `kvseo audit <url>` first.", code=1)
    return row.id


def _write(rendered: str, output: Path | None, aid: uuid.UUID, fmt: str) -> Path:
    if output is not None and output.suffix:  # an explicit filename
        path = output
    else:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        name = f"kvseo-report-{stamp}.{_EXT[fmt]}"
        path = (output or Path.cwd()) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return path


def _record_report(engine: Engine, aid: uuid.UUID, fmt: str, path: Path) -> None:
    # A single-audit report spans one point in time; range_start == range_end.
    with Session(engine) as session:
        run = session.get(AuditRunORM, aid)
        day = (run.fetched_at if run else "")[:10]
        session.add(
            ReportORM(
                range_start=day,
                range_end=day,
                template="single-audit",
                format=fmt,
                file_path=str(path),
            )
        )
        session.commit()


def _fail(message: str, *, code: int) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)
