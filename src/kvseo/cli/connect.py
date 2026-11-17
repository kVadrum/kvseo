"""``kvseo connect <source>`` — connect a read-only data source.

GSC (OAuth) and PSI (API key) since v0.1, CSV import as the escape hatch, and
the v0.2 sources: SerpBear (rankings), DataForSEO and OpenSEO (keyword data).
Every subcommand proves the credentials work before storing them (06 §4.4), so
a typo is caught here rather than by the next audit.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import typer

from kvseo.cli import _sources
from kvseo.cli._util import open_db
from kvseo.config.secrets import SecretStorageError, set_secret
from kvseo.connectors.base import ConnectorAuthError, ConnectorError
from kvseo.connectors.csv import CsvConnector, CsvImportError, ImportResult
from kvseo.connectors.dataforseo import DataForSeoConnector
from kvseo.connectors.gsc import GscConnector, GscSite
from kvseo.connectors.gsc_auth import (
    CLIENT_CONFIG_KEY,
    REFRESH_TOKEN_KEY,
    client_config,
    refresh_access_token,
    run_oauth_flow,
)
from kvseo.connectors.openseo import HOSTED_URL, OpenSeoConnector
from kvseo.connectors.serpbear import SerpBearConnector

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
    """Connect Google Search Console via OAuth; audits then pull its query data."""
    try:
        config = client_config(client_secrets)
        refresh_token = run_oauth_flow(config, port=port)
    except ConnectorAuthError as exc:
        # Auth failure is exit 4 (06 §2 / §4.4), not the generic 1.
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=4) from exc
    # The client config first: a stray config without a token is inert, while a
    # token whose client was never stored could not be refreshed.
    _store(CLIENT_CONFIG_KEY, json.dumps(config))
    _store(REFRESH_TOKEN_KEY, refresh_token)
    typer.echo("Connected Google Search Console.")
    try:
        sites = asyncio.run(_properties(refresh_token, config))
    except ConnectorError as exc:
        # The grant is stored and audits will retry; this was only the confirmation.
        typer.secho(f"Couldn't list your properties just now ({exc}).", fg=typer.colors.YELLOW, err=True)
        return
    if not sites:
        typer.secho(
            "This Google account has no Search Console properties, so audits will have no GSC data.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    typer.echo("Audits will pull query data for pages under:")
    for site in sites:
        typer.echo(f"   {site.site_url}")


async def _properties(refresh_token: str, config: dict[str, object]) -> list[GscSite]:
    token = await refresh_access_token(refresh_token, config)
    sites = await GscConnector(access_token=token).sites()
    return [s for s in sites if s.permission_level != "siteUnverifiedUser"]


@app.command()
def psi(
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Your PageSpeed Insights API key (free tier: 25k/day)."),
    ] = None,
) -> None:
    """Connect the PageSpeed Insights API by storing your API key."""
    if api_key:
        _store("psi:api_key", api_key)
        typer.echo("Stored your PSI API key — it'll be used on your next audit.")
    else:
        typer.echo(
            "No --api-key given. PSI works without a key at lower rate limits; "
            "pass --api-key to raise your quota."
        )


@app.command()
def serpbear(
    url: Annotated[str, typer.Option("--url", help="Your SerpBear instance, e.g. https://serpbear.example.com")],
    token: Annotated[str, typer.Option("--token", help="The APIKEY from your SerpBear .env.")],
) -> None:
    """Connect a self-hosted SerpBear for tracked-keyword rankings."""
    domains = _verify("SerpBear", SerpBearConnector(url, token).domains())
    _store(_sources.SERPBEAR_URL, url)
    _store(_sources.SERPBEAR_TOKEN, token)
    tracked = ", ".join(domains) if domains else "no domains yet"
    typer.echo(f"Connected SerpBear — tracking {tracked}. Audits of those domains read their rankings.")


@app.command()
def dataforseo(
    login: Annotated[str, typer.Option("--login", help="Your DataForSEO API login (email).")],
    password: Annotated[str, typer.Option("--password", help="Your DataForSEO API password.")],
    sandbox: Annotated[
        bool, typer.Option("--sandbox", help="Use DataForSEO's free sandbox (fake data) for testing.")
    ] = False,
) -> None:
    """Connect DataForSEO (your own account) for keyword volume, ideas and SERPs."""
    _verify("DataForSEO", DataForSeoConnector(login, password, sandbox=sandbox).health_check())
    _store(_sources.DATAFORSEO_LOGIN, login)
    _store(_sources.DATAFORSEO_PASSWORD, password)
    _store(_sources.DATAFORSEO_SANDBOX, "1" if sandbox else "0")
    typer.echo(
        "Connected DataForSEO" + (" (sandbox)" if sandbox else "")
        + ". Audits with --keyword fetch its search volume; DataForSEO bills per call."
    )


@app.command()
def openseo(
    token: Annotated[str, typer.Option("--token", help="An OpenSEO API key (oseo_…), from Settings → API keys.")],
    url: Annotated[
        str, typer.Option("--url", help="Your OpenSEO instance; the hosted app by default.")
    ] = HOSTED_URL,
) -> None:
    """Connect OpenSEO (hosted or self-hosted) for keyword metrics, over its MCP server."""
    projects = _verify("OpenSEO", OpenSeoConnector(token, url=url).projects())
    _store(_sources.OPENSEO_URL, url)
    _store(_sources.OPENSEO_TOKEN, token)
    names = ", ".join(p.name for p in projects) if projects else "none yet — create one in OpenSEO"
    typer.echo(f"Connected OpenSEO — projects: {names}.")


def _verify[T](name: str, check: Coroutine[Any, Any, T]) -> T:
    """Run a connector's check before anything is stored: bad credentials exit 4
    (06 §4.4), an unreachable host exits 5, and neither writes to the keyring."""
    try:
        return asyncio.run(check)
    except ConnectorAuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=4) from exc
    except ConnectorError as exc:
        typer.secho(f"couldn't verify {name}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=5) from exc


@app.command()
def csv(
    path: Annotated[Path, typer.Argument(help="Path to the CSV file to import.")],
    site: Annotated[
        str | None,
        typer.Option(
            "--site", help="The site the rows are for, e.g. https://kemek.net/ (required except for 'keywords')."
        ),
    ] = None,
    schema: Annotated[
        str,
        typer.Option(
            "--schema",
            help="queries (Search Console export) | rankings (rank-tracker export) | keywords (volume export).",
        ),
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
    """Import a CSV export into kvseo — queries, rankings or keyword volume (the no-API escape hatch)."""
    try:
        start = date.fromisoformat(date_from) if date_from else None
        end = date.fromisoformat(date_to) if date_to else None
    except ValueError as exc:
        typer.secho(f"bad date: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc
    if start and end and start > end:
        typer.secho(f"--from ({start}) is after --to ({end}).", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    if schema != "keywords" and not site:
        typer.secho(f"--site is required for '{schema}' imports.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    engine = open_db()
    connector = CsvConnector(engine=engine)
    try:
        result = asyncio.run(
            connector.import_csv(
                path,
                schema=schema,  # type: ignore[arg-type]  # validated inside; raises CsvImportError on bad value
                mapping=_parse_map(column_map),
                site=site or "",
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


def _store(key: str, value: str) -> None:
    # A backend-less box can't persist the credential; surface the reason and
    # exit on the environment-error code (3) rather than a raw traceback.
    try:
        set_secret(key, value)
    except SecretStorageError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=3) from exc


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
            f"Imported {result.imported} of {result.total_rows} rows into {result.table} "
            f"({result.failed} skipped)."
        )
    for err in result.errors[:10]:
        typer.secho(f"   row {err.row}: {err.error}", fg=typer.colors.YELLOW, err=True)
    if len(result.errors) > 10:
        typer.secho(f"   … and {len(result.errors) - 10} more.", fg=typer.colors.YELLOW, err=True)
