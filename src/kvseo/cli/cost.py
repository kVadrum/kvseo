"""``kvseo cost`` — summarize LLM advisor spend from stored runs (06 §4.9).

Read-only accounting: each advisor call recorded its own cost when it ran, so
this command spends no tokens — it just rolls the ``advisor_outputs`` rows up.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from typing import Annotated

import typer

from kvseo.cli._util import fail
from kvseo.core.cost import CostGroup, CostSummary, summarize_cost
from kvseo.storage.db import open_engine

_GROUP_BY = {"provider", "model", "day"}


def cost(
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only count runs on/after this date (YYYY-MM-DD)."),
    ] = None,
    month: Annotated[
        str | None,
        typer.Option("--month", help="Restrict to a single calendar month (YYYY-MM)."),
    ] = None,
    by: Annotated[
        str,
        typer.Option("--by", help="Break the total down by: provider | model | day."),
    ] = "provider",
    json_out: Annotated[bool, typer.Option("--json", help="Emit the summary as JSON.")] = False,
) -> None:
    """Summarize LLM advisor cost across stored runs."""
    if by not in _GROUP_BY:
        fail(f"unsupported --by '{by}' (provider | model | day).", code=2)
    if since and month:
        fail("pass --since or --month, not both.", code=2)
    start, end, label = _window(since, month)

    engine = open_engine()
    # `by` is validated against _GROUP_BY above, so it's a valid GroupBy here.
    summary = summarize_cost(engine, start=start, end=end, by=by)  # type: ignore[arg-type]

    if json_out:
        typer.echo(_as_json(summary, label))
    else:
        _print(summary, label)


def _window(since: str | None, month: str | None) -> tuple[date | None, date | None, str]:
    """Turn --since / --month into a ``[start, end)`` window and a display label."""
    if month:
        try:
            first = date.fromisoformat(f"{month}-01")
        except ValueError:
            fail(f"--month expects YYYY-MM, got '{month}'.", code=2)
        # First day of the following month — no dateutil dependency.
        nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
        return first, nxt, f"LLM cost — {month}"
    if since:
        try:
            start = date.fromisoformat(since)
        except ValueError:
            fail(f"--since expects YYYY-MM-DD, got '{since}'.", code=2)
        return start, None, f"LLM cost — since {since}"
    return None, None, "LLM cost — all time"


def _print(summary: CostSummary, label: str) -> None:
    typer.secho(label, bold=True)
    if summary.runs == 0:
        typer.echo("   No advisor runs recorded in this window.")
        return
    typer.echo(f"   Total: ${summary.total_usd:.2f}  ({_runs(summary.runs)})")
    typer.echo(f"\n   By {summary.by}:")
    _rows(summary.groups)
    typer.echo("\n   By prompt:")
    _rows(summary.by_prompt)


def _rows(groups: list[CostGroup]) -> None:
    for g in groups:
        typer.echo(f"     {g.key:<18} ${g.cost_usd:>8.2f}  ({_runs(g.runs)})")


def _runs(n: int) -> str:
    return f"{n} run" if n == 1 else f"{n} runs"


def _as_json(summary: CostSummary, label: str) -> str:
    return json.dumps(
        {
            "label": label,
            "total_usd": summary.total_usd,
            "runs": summary.runs,
            "by": summary.by,
            "groups": [asdict(g) for g in summary.groups],
            "by_prompt": [asdict(g) for g in summary.by_prompt],
            "start": summary.start.isoformat() if summary.start else None,
            "end": summary.end.isoformat() if summary.end else None,
        },
        indent=2,
    )
