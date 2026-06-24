"""``kvseo connect <source>`` — connect a read-only data source.

v0.1 ships GSC (OAuth) and PSI (API key); CSV import is the escape hatch. The
OpenSEO / SerpBear / DataForSEO connectors are v0.2 (ADR-004).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from kvseo.config.secrets import set_secret
from kvseo.connectors.base import ConnectorAuthError
from kvseo.connectors.gsc_auth import run_oauth_flow

app = typer.Typer(
    help="Connect kvseo to a read-only data source.", no_args_is_help=True
)


@app.command()
def gsc(
    client_secrets: Annotated[
        Path | None,
        typer.Option("--client-secrets", help="Path to an OAuth client_secrets.json."),
    ] = None,
    port: Annotated[
        int,
        typer.Option("--port", help="Local OAuth callback port (0 = auto-pick)."),
    ] = 0,
) -> None:
    """Connect Google Search Console via OAuth (stores a refresh token)."""
    try:
        refresh_token = run_oauth_flow(client_secrets=client_secrets, port=port)
    except ConnectorAuthError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1) from exc
    set_secret("gsc:refresh_token", refresh_token)
    typer.echo("Connected Google Search Console — refresh token stored.")


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
