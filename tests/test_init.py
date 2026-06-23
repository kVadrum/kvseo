"""`kvseo init` creates config + database, and is idempotent."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kvseo.cli import app

runner = CliRunner()


def test_init_creates_config_and_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("KVSEO_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.stdout

    cfg = tmp_path / "cfg" / "config.toml"
    db = tmp_path / "data" / "kvseo.db"
    assert cfg.exists()
    assert db.exists()

    # Migrations ran (alembic_version stamped), the schema exists, WAL is on.
    conn = sqlite3.connect(db)
    try:
        version = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()
    finally:
        conn.close()
    assert version is not None and version[0] == "0001"
    assert "audit_runs" in tables
    assert journal_mode[0].lower() == "wal"


def test_init_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("KVSEO_DATA_DIR", str(tmp_path / "data"))
    assert runner.invoke(app, ["init"]).exit_code == 0
    assert runner.invoke(app, ["init"]).exit_code == 0
