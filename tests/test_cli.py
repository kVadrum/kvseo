"""Smoke tests for the CLI shell."""

from __future__ import annotations

from typer.testing import CliRunner

from kvseo import __version__
from kvseo.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "kvseo" in result.stdout
