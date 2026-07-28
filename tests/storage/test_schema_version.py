"""The schema-version pin: refuse a database newer than the running package.

07-data-model.md §4. Only the *ahead* direction is guarded — a database behind
the package is migrated forward automatically on every command, so it never
reaches a user as an error.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.storage.db import (
    HEAD_REVISION,
    KNOWN_REVISIONS,
    SchemaVersionError,
    check_schema_version,
    get_engine,
    migrate,
    stored_revision,
)

runner = CliRunner()


def _set_revision(db: Path, revision: str) -> None:
    engine = get_engine(db)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": revision})
    engine.dispose()


def test_known_revisions_match_alembic(tmp_path: Path) -> None:
    """The hand-maintained tuple must match Alembic's own view of the scripts.

    KNOWN_REVISIONS is a literal so the startup check avoids importing alembic
    (~100ms on every command). This test is what keeps that shortcut honest:
    add a migration without extending the tuple and it fails here.
    """
    from alembic.script import ScriptDirectory

    from kvseo.storage.db import _alembic_config

    script = ScriptDirectory.from_config(_alembic_config(tmp_path / "unused.db"))
    revisions = {rev.revision for rev in script.walk_revisions()}

    assert revisions == set(KNOWN_REVISIONS)
    assert script.get_current_head() == HEAD_REVISION


def test_fresh_database_migrates_to_head(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    assert stored_revision(db) == HEAD_REVISION


def test_absent_database_is_not_a_mismatch(tmp_path: Path) -> None:
    """Nothing to compare against yet — migrate() is one call away."""
    missing = tmp_path / "nope.db"
    assert stored_revision(missing) is None
    check_schema_version(missing)  # must not raise


def test_database_without_alembic_version_table(tmp_path: Path) -> None:
    """A stray/empty SQLite file reads as unmigrated, not as a mismatch."""
    db = tmp_path / "empty.db"
    engine = get_engine(db)
    engine.connect().close()
    engine.dispose()

    assert stored_revision(db) is None
    check_schema_version(db)  # must not raise


def test_newer_database_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    _set_revision(db, "9999")

    with pytest.raises(SchemaVersionError) as exc:
        check_schema_version(db)
    message = str(exc.value)
    assert "9999" in message and HEAD_REVISION in message
    assert "pip install -U kvseo" in message


def test_migrate_refuses_a_newer_database(tmp_path: Path) -> None:
    """The guard sits in migrate(), so `kvseo init` is covered too — otherwise
    Alembic raises its own 'can't locate revision' error instead."""
    db = tmp_path / "kvseo.db"
    migrate(db)
    _set_revision(db, "9999")

    with pytest.raises(SchemaVersionError):
        migrate(db)


def test_cli_exits_3_on_newer_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06 §2: configuration error, not a traceback and not a usage error."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))

    assert runner.invoke(app, ["init"]).exit_code == 0
    _set_revision(data_dir / "kvseo.db", "9999")

    result = runner.invoke(app, ["cost"])
    assert result.exit_code == 3
    assert "9999" in result.output

    # init is guarded on the same path, not just the query commands.
    assert runner.invoke(app, ["init"]).exit_code == 3


def test_version_and_help_survive_an_unusable_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard gates database work, not the whole CLI — a user with a broken
    database must still be able to see what version they are running."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))

    assert runner.invoke(app, ["init"]).exit_code == 0
    _set_revision(data_dir / "kvseo.db", "9999")

    assert runner.invoke(app, ["--version"]).exit_code == 0
    assert runner.invoke(app, ["--help"]).exit_code == 0
