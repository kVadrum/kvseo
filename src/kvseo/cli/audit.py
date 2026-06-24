"""``kvseo audit <url>`` — the headline command (04-audit-engine.md §10).

Runs the deterministic audit, then (unless ``--no-advisor``) the LLM advisor
against the stored result, then writes a self-contained report. The audit and
the advisor are separate stages so a missing LLM key degrades to a raw audit
rather than a failure.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.engine import Engine

from kvseo.config import paths
from kvseo.config.secrets import get_secret
from kvseo.config.settings import Settings
from kvseo.connectors.psi import PsiConnector
from kvseo.core.advisor.client import AdvisorRun, prioritize
from kvseo.core.advisor.context import AdvisorError
from kvseo.core.audit.engine import AuditResult, run_audit
from kvseo.core.report.renderer import render
from kvseo.storage.db import get_engine, migrate

_REPORT_FORMATS = {"html", "md", "json", "all", "none"}


def audit(
    url: Annotated[str, typer.Argument(help="The URL to audit.")],
    keyword: Annotated[
        str | None,
        typer.Option("--keyword", "-k", help="Target keyword to check title/heading against."),
    ] = None,
    no_cwv: Annotated[
        bool, typer.Option("--no-cwv", help="Skip the PageSpeed Insights / Core Web Vitals pull.")
    ] = False,
    no_advisor: Annotated[
        bool, typer.Option("--no-advisor", help="Skip the LLM advisor (raw audit only).")
    ] = False,
    report_format: Annotated[
        str | None,
        typer.Option("--format", "-f", help="Report file: html | md | json | all | none."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Where to write the report (default: ./)."),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the full audit as JSON.")] = False,
) -> None:
    """Run an on-page audit against a URL."""
    fmt = (report_format or ("none" if json_out else "html")).lower()
    if fmt not in _REPORT_FORMATS:
        typer.secho(f"unsupported --format '{fmt}'.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    db = paths.db_path()
    migrate(db)  # ensure the schema exists even before `kvseo init`
    engine = get_engine(db)
    psi = None if no_cwv else PsiConnector(api_key=_psi_key(), engine=engine)
    result = asyncio.run(run_audit(url, db_engine=engine, keyword=keyword, psi=psi))

    advisor: AdvisorRun | None = None
    if result.status == "complete" and not no_advisor:
        advisor = _run_advisor(result.id, engine, quiet=json_out)

    written: list[Path] = []
    if result.status == "complete" and fmt != "none":
        written = _write_reports(result, fmt, output, engine)

    if json_out:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_summary(result, advisor, written)
    raise typer.Exit(0 if result.status == "complete" else 1)


def _run_advisor(audit_id: uuid.UUID, engine: Engine, quiet: bool) -> AdvisorRun | None:
    settings = Settings.load(paths.config_file())
    try:
        return asyncio.run(prioritize(audit_id, engine=engine, settings=settings))
    except AdvisorError as exc:
        # No key / oversized context / not-ready: the audit still stands. Surface
        # the reason once, then carry on — the advisor can be re-run later.
        if not quiet:
            typer.secho(f"advisor skipped — {exc}", fg=typer.colors.YELLOW, err=True)
        return None


def _write_reports(
    result: AuditResult, fmt: str, output: Path | None, engine: Engine
) -> list[Path]:
    targets = ["html", "md"] if fmt == "all" else [fmt]
    multi = len(targets) > 1
    written: list[Path] = []
    for target in targets:
        content = (
            result.model_dump_json(indent=2)
            if target == "json"
            else render(result.id, target, engine=engine)
        )
        written.append(_write_file(content, output, result.id, target, multi=multi))
    return written


def _write_file(content: str, output: Path | None, audit_id: uuid.UUID, ext: str, *, multi: bool) -> Path:
    if output is not None and output.suffix:
        # An explicit filename is honored verbatim for a single format; for
        # `--format all` the two outputs get distinct suffixes off that name.
        path = output.with_suffix(f".{ext}") if multi else output
    else:
        path = (output or Path.cwd()) / f"kvseo-report-{audit_id}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _psi_key() -> str | None:
    return get_secret("psi:api_key") or os.environ.get("PSI_API_KEY")


def _print_summary(result: AuditResult, advisor: AdvisorRun | None, written: list[Path]) -> None:
    if result.status != "complete":
        typer.secho(f"audit failed: {result.failure_reason}", fg=typer.colors.RED, err=True)
        return

    counts: dict[str, int] = {}
    for check in result.checks:
        counts[check.verdict] = counts.get(check.verdict, 0) + 1

    typer.echo(f"kvseo audit — {result.url}")
    typer.echo(f"   Score: {result.score}/100")
    typer.echo(
        f"   {counts.get('pass', 0)} passed · {counts.get('warn', 0)} warned · "
        f"{counts.get('fail', 0)} failed · {counts.get('skip', 0)} skipped"
    )

    fails = [c for c in result.checks if c.verdict == "fail"]
    warns = [c for c in result.checks if c.verdict == "warn"]
    if fails:
        typer.echo("\n   Failures:")
        for c in fails:
            # The leading glyph is a deliberate status marker in the CLI summary.
            typer.secho(f"     × {c.check_id:<24} {c.message}", fg=typer.colors.RED)  # noqa: RUF001
    if warns:
        typer.echo("\n   Warnings:")
        for c in warns:
            typer.secho(f"     ! {c.check_id:<24} {c.message}", fg=typer.colors.YELLOW)

    if advisor and advisor.status == "success" and advisor.output and advisor.output.actions:
        top = advisor.output.actions[0]
        typer.echo("\n   Advisor top recommendation:")
        typer.secho(
            f"     {top.rank}. {top.title} "
            f"({top.expected_impact} impact, {top.effort} effort)",
            fg=typer.colors.CYAN,
        )

    for path in written:
        typer.echo(f"\n   Report: {path}")
    typer.echo(f"   Run ID: {result.id}")
