"""``kvseo report`` — render a stored audit to a report (built in v0.1)."""

from __future__ import annotations

from typing import Annotated

import typer


def report(
    report_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Report format: md | html."),
    ] = "html",
) -> None:
    """Render a self-contained report from stored audit data."""
    typer.secho(
        "kvseo report is not implemented yet — the report renderer is in "
        "active build toward v0.1.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=1)
