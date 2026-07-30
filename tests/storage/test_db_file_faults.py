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
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.storage.db import (
    HEAD_REVISION,
    DatabaseFileError,
    _file_fault_code,
    backup_to,
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


def test_sqlalchemy_wrapped_errors_are_classified_through_orig(tmp_path: Path) -> None:
    """The ``orig`` unwrap exists for the Alembic path, so pin it on a real wrapper.

    Only ``migrate()`` classifies a SQLAlchemy-wrapped error, and its own tests
    never get that far — ``check_schema_version`` rejects a bad file on the cheap
    probe first. Without this, the unwrap was asserted by docstring only.
    """
    db = _not_a_database(tmp_path / "kvseo.db")
    engine = get_engine(db)
    try:
        with pytest.raises(SQLAlchemyError) as exc, engine.connect() as conn:
            conn.execute(text("SELECT version_num FROM alembic_version"))
    finally:
        engine.dispose()

    assert not isinstance(exc.value, sqlite3.Error), "expected SQLAlchemy's wrapper, not the driver error"
    assert _file_fault_code(exc.value) == 26  # SQLITE_NOTADB, reached via .orig


def test_backup_to_blames_the_destination_when_it_cannot_be_opened(tmp_path: Path) -> None:
    """A destination that will not open must not indict a healthy source.

    SQLITE_CANTOPEN is the same code either side of the copy, so attributing it
    by position rather than by path told the user to fix $KVSEO_DATA_DIR when the
    problem was the --output they had just passed.

    A directory as the destination is the portable way to provoke this: opening
    one as a database fails on every platform. The CLI-level version of this test
    has to make a directory unwritable instead, which Windows does not honour.
    """
    source = tmp_path / "kvseo.db"
    migrate(source)
    dest = tmp_path / "i-am-a-directory"
    dest.mkdir()

    with pytest.raises(DatabaseFileError) as exc:
        backup_to(source, dest)

    message = str(exc.value)
    assert str(dest) in message
    assert str(source) not in message, "the source is healthy; naming it sends the user to the wrong fix"
    assert "KVSEO_DATA_DIR" not in message


def test_backup_to_blames_the_destination_when_the_copy_writes_to_garbage(tmp_path: Path) -> None:
    """A destination that exists as a non-database must not indict the source.

    This is the earlier misattribution with the sides swapped, and the advice it
    produced was worse: it told a user whose *destination* was a stray text file
    to delete their live database. An existing regular file opens lazily, so the
    fault surfaces mid-copy — the branch that has to decide which side is at
    fault. The CLI refuses an existing --output before reaching here, but this is
    a public storage function and that shield is one flag away from gone.
    """
    source = tmp_path / "kvseo.db"
    migrate(source)
    dest = tmp_path / "precious-notes.txt"
    dest.write_text("the user's precious notes", encoding="utf-8")

    with pytest.raises(DatabaseFileError) as exc:
        backup_to(source, dest)

    message = str(exc.value)
    assert str(dest) in message
    assert str(source) not in message, "blaming the source tells the user to delete a healthy database"
    assert "re-run `kvseo init`" not in message


def test_backup_to_blames_the_source_when_the_copy_reads_garbage(tmp_path: Path) -> None:
    """The mid-copy handler: a bad source only shows up once backup() reads it.

    ``sqlite3.connect`` is lazy, so opening the source succeeds on any path and
    the destination opens fine — the failure lands in the copy itself. Called
    directly rather than through the CLI, which probes the source first and would
    never let this path run.
    """
    source = _not_a_database(tmp_path / "kvseo.db")
    dest = tmp_path / "backup.db"

    with pytest.raises(DatabaseFileError) as exc:
        backup_to(source, dest)

    message = str(exc.value)
    assert str(source) in message, "the copy failed reading the source, so the source is what to name"
    assert "could not write the backup" not in message


# --- Sub-header-size files: SQLite would initialise over them ---------------


def test_a_one_byte_file_is_refused_and_left_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite treats a tiny file as an empty database and writes over it.

    Measured before the header check: a 1-byte file at the database path was
    replaced by a 144 KiB kvseo database, exit 0 — silent data loss on a file the
    user owns. 50 bytes and up already failed with SQLITE_NOTADB, so the gap was
    only below SQLite's own header threshold.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data_dir))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    stray = data_dir / "kvseo.db"
    stray.write_bytes(b"x")

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 3, result.output
    assert "not a usable kvseo database" in result.output
    assert stray.read_bytes() == b"x", "init overwrote a file that was not a database"


def test_a_zero_byte_file_is_still_treated_as_a_new_database(tmp_path: Path) -> None:
    """The header check must not reject what `touch` leaves behind."""
    db = tmp_path / "kvseo.db"
    db.touch()

    assert stored_revision(db) is None
    migrate(db)
    assert stored_revision(db) == HEAD_REVISION


# --- The CLI contract ------------------------------------------------------


@pytest.mark.parametrize("command", [["init"], ["cost"], ["db", "migrate"], ["db", "backup"], ["db", "vacuum"]])
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
