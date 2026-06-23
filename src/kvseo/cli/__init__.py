"""kvseo command-line interface (Typer).

This module is the only entry point in v0.1. It is a thin shell over
``kvseo.core``; all business logic lives below the CLI (see 02-architecture.md
§2 for the layering rule).
"""

from __future__ import annotations

from typing import Annotated

import typer

from kvseo import __version__
from kvseo.cli import audit, config, connect, init, report

app = typer.Typer(
    name="kvseo",
    help="AI-native SEO copilot for solo operators and small agencies.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"kvseo {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the kvseo version and exit.",
            is_eager=True,
            callback=_version_callback,
        ),
    ] = False,
) -> None:
    """kvseo — the AI layer over your existing SEO stack."""


# --- Command wiring -------------------------------------------------------
# Top-level verbs.
app.command()(init.init)
app.command()(audit.audit)
app.command()(report.report)
# Sub-command groups.
app.add_typer(connect.app, name="connect")
app.add_typer(config.app, name="config")
