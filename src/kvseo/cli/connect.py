"""``kvseo connect <source>`` — connect a read-only data source.

v0.1 ships GSC (OAuth) and PSI (API key); CSV import is the escape hatch. The
OpenSEO / SerpBear / DataForSEO connectors are v0.2 (ADR-004). Subcommands are
stubbed so the CLI surface (handoff §3 item 4 / Q3) is reviewable now.
"""

from __future__ import annotations

from typing import Annotated

import typer

from kvseo.config.secrets import set_secret

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
def psi(
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Your PageSpeed Insights API key (free tier: 25k/day)."),
    ] = None,
) -> None:
    """Connect the PageSpeed Insights API by storing your API key."""
    if api_key:
        set_secret("psi:api_key", api_key)
        typer.echo("Stored your PSI API key — it'll be used on your next audit.")
    else:
        typer.echo(
            "No --api-key given. PSI works without a key at lower rate limits; "
            "pass --api-key to raise your quota."
        )
