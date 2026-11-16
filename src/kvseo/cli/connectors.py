"""``kvseo connectors`` — which data sources this machine is connected to (06 §4.5).

Reads only what is stored; it makes no network call, so it is safe to run
offline and costs nothing. ``kvseo connect <source>`` is what verifies.
"""

from __future__ import annotations

import json
import os
from typing import Annotated

import typer

from kvseo.cli import _sources
from kvseo.config.secrets import get_secret
from kvseo.connectors.gsc_auth import REFRESH_TOKEN_KEY


def connectors(json_out: Annotated[bool, typer.Option("--json", help="Emit the list as JSON.")] = False) -> None:
    """List the data sources kvseo is connected to."""
    rows = _status()
    if json_out:
        typer.echo(json.dumps([{"name": n, "configured": c, "detail": d} for n, c, d in rows], indent=2))
        return
    typer.echo("Configured connectors:")
    for name, configured, detail in rows:
        mark = "✓" if configured else "·"
        typer.echo(f"  {mark} {name:<11} {detail}")


def _status() -> list[tuple[str, bool, str]]:
    psi_key = bool(get_secret("psi:api_key") or os.environ.get("PSI_API_KEY"))
    serpbear = _sources.serpbear()
    dfs = _sources.dataforseo()
    oseo = _sources.openseo()
    sandbox = get_secret(_sources.DATAFORSEO_SANDBOX) == "1"
    return [
        ("gsc", get_secret(REFRESH_TOKEN_KEY) is not None, _say(get_secret(REFRESH_TOKEN_KEY), "connected")),
        # PSI works keyless at lower limits, so it is always usable.
        ("psi", True, "(api key set)" if psi_key else "(keyless — lower rate limits)"),
        ("serpbear", serpbear is not None, _say(serpbear, _sources.stored(_sources.SERPBEAR_URL, "SERPBEAR_URL"))),
        ("dataforseo", dfs is not None, _say(dfs, "(sandbox)" if sandbox else "connected")),
        ("openseo", oseo is not None, _say(oseo, _sources.stored(_sources.OPENSEO_URL, "OPENSEO_URL"))),
    ]


def _say(connected: object, detail: str | None) -> str:
    return (detail or "connected") if connected else "(not configured)"
