"""A bad file at the database path exits 3 with a message, not 1 with a traceback.

06 §2 reserves exit 3 for configuration errors. A path holding garbage, a
corrupt database, or a directory is exactly that — the environment is wrong, and
no amount of migrating forward fixes any of them. The discrimination that makes
this safe is narrow on purpose: only SQLITE_NOTADB / SQLITE_CORRUPT /
SQLITE_CANTOPEN are treated as file faults, so an unmigrated database and a
genuine migration failure keep the behaviour they had.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.storage.db import (
    DatabaseFileError,
    _file_fault_code,
    check_schema_version,
    get_engine,
    migrate,
    stored_revision,
)

runner = CliRunner()


def _not_a_database(path: Path) -> Path:
    """A file whose very header is wrong — SQLITE_NOTADB."""
    path.write_text("this is not a sqlite database, it is a note to self\n", encoding="utf-8")
    return path


def _corrupt_database(path: Path) -> Path:
    """A real database with its pages trashed — valid header, SQLITE_CORRUPT.

    Distinct from ``_not_a_database`` because it gets *further*: the header
    passes, so the failure surfaces mid-read rather than on the first pragma.
    """
    migrate(path)
    raw = bytearray(path.read_bytes())
    for i in range(100, len(raw)):
        raw[i] = 0xFF
    path.write_bytes(bytes(raw))
    return path


# --- The probe rejects a bad file before alembic is ever imported -----------


def test_not_a_database_is_rejected_by_the_probe(tmp_path: Path) -> None:
    db = _not_a_database(tmp_path / "kvseo.db")

    with pytest.raises(DatabaseFileError) as exc:
        stored_revision(db)

    message = str(exc.value)
    assert str(db) in message
    assert "kvseo init" in message


def test_corrupt_database_is_rejected(tmp_path: Path) -> None:
    db = _corrupt_database(tmp_path / "kvseo.db")

    with pytest.raises(DatabaseFileError):
        stored_revision(db)


def test_directory_at_the_database_path_is_rejected(tmp_path: Path) -> None:
    """SQLITE_CANTOPEN gets its own wording — "not a database" would misdiagnose."""
    db = tmp_path / "kvseo.db"
    db.mkdir()

    with pytest.raises(DatabaseFileError) as exc:
        stored_revision(db)

    message = str(exc.value)
    assert "cannot open" in message
    assert "KVSEO_DATA_DIR" in message


def test_schema_check_surfaces_the_file_fault(tmp_path: Path) -> None:
    """The guard every command runs must not read a bad file as "unmigrated"."""
    db = _not_a_database(tmp_path / "kvseo.db")

    with pytest.raises(DatabaseFileError):
        check_schema_version(db)


def test_migrate_refuses_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    db = _not_a_database(tmp_path / "kvseo.db")

    with pytest.raises(DatabaseFileError):
        migrate(db)


# --- What must NOT be treated as a file fault ------------------------------


def test_unmigrated_database_still_reads_as_none(tmp_path: Path) -> None:
    """The regression that matters: a valid file without our tables is fine.

    SQLITE_ERROR ("no such table") shares the DatabaseError family with the
    faults above. Reading it as a fault would make every fresh `kvseo init`
    exit 3.
    """
    db = tmp_path / "fresh.db"
    engine = get_engine(db)
    engine.connect().close()
    engine.dispose()

    assert stored_revision(db) is None
    check_schema_version(db)  # must not raise


def test_query_errors_are_not_file_faults(tmp_path: Path) -> None:
    """A real driver error on a healthy database must classify as "not a fault"."""
    db = tmp_path / "kvseo.db"
    migrate(db)

    conn = sqlite3.connect(db)
    try:
        with pytest.raises(sqlite3.Error) as exc:
            conn.execute("SELECT * FROM no_such_table")
    finally:
        conn.close()

    assert _file_fault_code(exc.value) is None


def test_extended_result_codes_are_recognised() -> None:
    """SQLITE_CORRUPT_VTAB (11 | 1 << 8) must classify as SQLITE_CORRUPT.

    SQLite reports extended codes in the low byte + a subtype above it, so the
    predicate masks. Constructed directly: provoking an extended corruption
    code from a real file is not reliably reproducible.
    """
    exc = sqlite3.DatabaseError("database disk image is malformed")
    exc.sqlite_errorcode = 11 | (1 << 8)
    exc.sqlite_errorname = "SQLITE_CORRUPT_VTAB"

    assert _file_fault_code(exc) == 11


def test_non_sqlite_exceptions_are_not_file_faults() -> None:
    assert _file_fault_code(ValueError("unrelated")) is None


# --- The CLI contract ------------------------------------------------------


@pytest.mark.parametrize("command", [["init"], ["cost"]])
def test_cli_exits_3_on_a_file_that_is_not_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    _not_a_database(data_dir / "kvseo.db")

    result = runner.invoke(app, command)

    assert result.exit_code == 3, result.output
    assert "not a usable kvseo database" in result.output


def test_version_and_help_survive_a_file_that_is_not_a_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal gates database work, not the whole CLI (as for the schema pin)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    _not_a_database(data_dir / "kvseo.db")

    assert runner.invoke(app, ["--version"]).exit_code == 0
    assert runner.invoke(app, ["--help"]).exit_code == 0
