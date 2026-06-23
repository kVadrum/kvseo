"""``kvseo audit <url>`` — the headline command (engine lands in the v0.1 build).

The signature here fixes the CLI surface under review (handoff §3 item 4, risk
R10): verb-noun, a single positional URL, ``--no-advisor`` to get a raw audit.
The on-page engine, CWV, GSC context, advisor, and persistence are built
against 04-audit-engine.md.
"""

from __future__ import annotations

from typing import Annotated

import typer


def audit(
    url: Annotated[str, typer.Argument(help="The URL to audit.")],
    no_advisor: Annotated[
        bool,
        typer.Option("--no-advisor", help="Skip the AI advisor; produce a raw audit."),
    ] = False,
) -> None:
    """Run an on-page audit against a URL."""
    typer.secho(
        "kvseo audit is not implemented yet — the audit engine is in active "
        "build toward v0.1.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=1)
