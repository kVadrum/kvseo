"""``kvseo connect <source>`` — connect a read-only data source.

v0.1 ships GSC (OAuth) and PSI (API key); CSV import is the escape hatch. The
OpenSEO / SerpBear / DataForSEO connectors are v0.2 (ADR-004). Subcommands are
stubbed so the CLI surface (handoff §3 item 4 / Q3) is reviewable now.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    help="Connect kvseo to a read-only data source.", no_args_is_help=True
)


def _not_yet(name: str) -> None:
    typer.secho(
        f"connector '{name}' is not implemented yet — connectors are in active "
        "build toward v0.1.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    raise typer.Exit(code=1)


@app.command()
def gsc() -> None:
    """Connect Google Search Console (OAuth)."""
    _not_yet("gsc")


@app.command()
def psi() -> None:
    """Connect the PageSpeed Insights API (API key)."""
    _not_yet("psi")
