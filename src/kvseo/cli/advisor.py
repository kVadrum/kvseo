"""``kvseo advisor <subcommand>`` — run or inspect the LLM advisor (06 §4.6).

The advisor runs against a *stored* audit, so it's cheap to re-run (no re-fetch,
no re-audit) — handy after an audit completed with ``--no-advisor`` or while
iterating on prompts. ``run`` calls the model and persists; ``show`` reads back
the most recent stored run without spending a token.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from kvseo.config import paths
from kvseo.config.settings import Settings
from kvseo.core.advisor.client import AdvisorRun, ContextOverflowError, latest_run, prioritize
from kvseo.core.advisor.context import AdvisorError
from kvseo.storage.db import get_engine, migrate

app = typer.Typer(help="Run or inspect the AI advisor.", no_args_is_help=True)


@app.command()
def run(
    audit_id: Annotated[str, typer.Argument(help="The audit run ID to advise on.")],
    provider: Annotated[
        str | None, typer.Option("--provider", help="Override the configured LLM provider.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Override the configured LLM model.")
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the advisor output as JSON.")] = False,
) -> None:
    """Run the prioritization advisor against a stored audit."""
    aid = _parse_id(audit_id)
    settings = _settings(provider, model)
    engine = get_engine(_db())

    try:
        result = asyncio.run(prioritize(aid, engine=engine, settings=settings))
    except ContextOverflowError as exc:
        _fail(str(exc), code=6)
    except AdvisorError as exc:
        # "no API key" is an auth problem (exit 4); everything else (audit not
        # found / not complete) is a general error (exit 1).
        _fail(str(exc), code=4 if "no API key" in str(exc) else 1)

    _emit(result, json_out=json_out)
    raise typer.Exit(_exit_for(result.status))


@app.command()
def show(
    audit_id: Annotated[str, typer.Argument(help="The audit run ID to show advice for.")],
    json_out: Annotated[bool, typer.Option("--json", help="Emit the advisor output as JSON.")] = False,
) -> None:
    """Print the most recent advisor output for an audit."""
    aid = _parse_id(audit_id)
    engine = get_engine(_db())
    result = latest_run(aid, engine)
    if result is None:
        _fail(f"no advisor run for audit {aid} — run `kvseo advisor run {aid}` first.", code=1)
    _emit(result, json_out=json_out)


# --- Shared helpers -------------------------------------------------------


def _db() -> Path:
    db = paths.db_path()
    migrate(db)
    return db


def _settings(provider: str | None, model: str | None) -> Settings:
    settings = Settings.load(paths.config_file())
    if provider:
        settings.advisor.provider = provider
    if model:
        settings.advisor.model = model
    return settings


def _parse_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        _fail(f"'{value}' is not a valid audit ID.", code=2)


def _fail(message: str, *, code: int) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=code)


def _exit_for(status: str) -> int:
    # success → 0; the model answered but failed validation → 6; the call itself
    # failed (provider down / bad key) → 5 (connector unavailable).
    return {"success": 0, "invalid_output": 6, "failed": 5}.get(status, 1)


def _emit(result: AdvisorRun, *, json_out: bool) -> None:
    if json_out:
        typer.echo(result.model_dump_json(indent=2))
        return
    if result.status != "success" or result.output is None:
        detail = result.error or "no parseable output"
        typer.secho(f"advisor {result.status}: {detail}", fg=typer.colors.YELLOW, err=True)
        typer.echo(f"   Run ID: {result.id}")
        return
    _print_prioritization(result)


def _print_prioritization(result: AdvisorRun) -> None:
    out = result.output
    assert out is not None
    typer.secho("kvseo advisor — prioritization", bold=True)
    if out.summary:
        typer.echo(f"\n{out.summary}")
    typer.echo("\nTop actions:")
    for action in out.actions:
        typer.secho(
            f"  {action.rank}. {action.title}",
            fg=typer.colors.CYAN,
        )
        typer.echo(
            f"     impact: {action.expected_impact} · effort: {action.effort} · "
            f"{action.category} · evidence: {', '.join(action.evidence)}"
        )
        typer.echo(f"     {action.description}")
    if out.things_going_well:
        typer.echo("\nGoing well:")
        for item in out.things_going_well:
            typer.echo(f"  + {item}")
    if out.cautions:
        typer.echo("\nCautions:")
        for item in out.cautions:
            typer.echo(f"  ! {item}")
    cost = f"${result.estimated_cost_usd:.4f}" if result.estimated_cost_usd is not None else "n/a"
    typer.echo(f"\n   Run ID: {result.id}  ·  cost: {cost}")
