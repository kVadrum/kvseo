"""``kvseo audit <url>`` — the headline command (04-audit-engine.md §10).

Verb-noun, single positional URL. Runs the engine, persists, and prints a
summary (or ``--json``). The advisor and the rendered report are separate
modules; this command produces the deterministic audit.
"""

from __future__ import annotations

import asyncio
import os
from typing import Annotated

import typer

from kvseo.config import paths
from kvseo.config.secrets import get_secret
from kvseo.connectors.psi import PsiConnector
from kvseo.core.audit.engine import AuditResult, run_audit
from kvseo.storage.db import get_engine, migrate


def audit(
    url: Annotated[str, typer.Argument(help="The URL to audit.")],
    keyword: Annotated[
        str | None,
        typer.Option("--keyword", "-k", help="Target keyword to check title/heading against."),
    ] = None,
    no_cwv: Annotated[
        bool, typer.Option("--no-cwv", help="Skip the PageSpeed Insights / Core Web Vitals pull.")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the full audit as JSON.")] = False,
) -> None:
    """Run an on-page audit against a URL."""
    db = paths.db_path()
    migrate(db)  # ensure the schema exists even before `kvseo init`
    engine = get_engine(db)
    psi = None if no_cwv else PsiConnector(api_key=_psi_key(), engine=engine)
    result = asyncio.run(run_audit(url, db_engine=engine, keyword=keyword, psi=psi))

    if json_out:
        typer.echo(result.model_dump_json(indent=2))
    else:
        _print_summary(result)
    raise typer.Exit(0 if result.status == "complete" else 1)


def _psi_key() -> str | None:
    return get_secret("psi:api_key") or os.environ.get("PSI_API_KEY")


def _print_summary(result: AuditResult) -> None:
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

    typer.echo(f"\n   Run ID: {result.id}")
