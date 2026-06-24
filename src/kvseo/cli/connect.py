"""``kvseo connect <source>`` — connect a read-only data source.

v0.1 ships GSC (OAuth) and PSI (API key); CSV import is the escape hatch. The
OpenSEO / SerpBear / DataForSEO connectors are v0.2 (ADR-004).
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from kvseo.config import paths
from kvseo.config.secrets import set_secret
from kvseo.connectors.base import ConnectorAuthError
from kvseo.connectors.csv import CsvConnector, CsvImportError, ImportResult
from kvseo.connectors.gsc_auth import run_oauth_flow
from kvseo.storage.db import get_engine, migrate

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


@app.command()
def csv(
    path: Annotated[Path, typer.Argument(help="Path to the CSV file to import.")],
    site: Annotated[
        str,
        typer.Option("--site", help="GSC property the rows belong to, e.g. https://kemek.net/."),
    ],
    schema: Annotated[
        str,
        typer.Option("--schema", help="Row schema. v0.1: 'queries' (Search Console export)."),
    ] = "queries",
    page: Annotated[
        str | None,
        typer.Option("--page", help="Fill the page column for query-only exports with no page."),
    ] = None,
    date_from: Annotated[
        str | None,
        typer.Option("--from", help="Start of the row date range (YYYY-MM-DD). Default: today."),
    ] = None,
    date_to: Annotated[
        str | None,
        typer.Option("--to", help="End of the row date range (YYYY-MM-DD). Default: today."),
    ] = None,
    column_map: Annotated[
        list[str] | None,
        typer.Option("--map", help="Map a field to a header: --map page=Address (repeatable)."),
    ] = None,
) -> None:
    """Import a Search Console CSV export into kvseo (the no-API escape hatch)."""
    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        typer.secho(f"bad date: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    db = paths.db_path()
    migrate(db)
    engine = get_engine(db)
    connector = CsvConnector(engine=engine)
    try:
        result = asyncio.run(
            connector.import_csv(
                path,
                schema=schema,  # type: ignore[arg-type]  # validated inside; raises CsvImportError on bad value
                mapping=_parse_map(column_map),
                site=site,
                default_page=page,
                date_start=start,
                date_end=end,
            )
        )
    except CsvImportError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=6) from exc

    _print_import(result)
    raise typer.Exit(0 if result.committed else 6)


def _parse_map(pairs: list[str] | None) -> dict[str, str] | None:
    if not pairs:
        return None
    mapping: dict[str, str] = {}
    for pair in pairs:
        field_name, _, header = pair.partition("=")
        if not _ or not field_name.strip():
            raise typer.BadParameter(f"--map expects field=header, got '{pair}'")
        mapping[field_name.strip()] = header.strip()
    return mapping


def _print_import(result: ImportResult) -> None:
    if not result.committed:
        typer.secho(
            f"Import rolled back — {result.failed} of {result.total_rows} rows failed "
            "validation (too many to be a clean import).",
            fg=typer.colors.RED,
            err=True,
        )
    else:
        typer.echo(
            f"Imported {result.imported} of {result.total_rows} rows into gsc_queries "
            f"({result.failed} skipped)."
        )
    for err in result.errors[:10]:
        typer.secho(f"   row {err.row}: {err.error}", fg=typer.colors.YELLOW, err=True)
    if len(result.errors) > 10:
        typer.secho(f"   … and {len(result.errors) - 10} more.", fg=typer.colors.YELLOW, err=True)
