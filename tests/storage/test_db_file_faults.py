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
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.storage.db import (
    HEAD_REVISION,
    DatabaseBusyError,
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


def test_unmigrated_database_still_reads_as_none(tmp_path: Path, unversioned_db: Callable[[Path], Path]) -> None:
    """The regression that matters: a valid file without our tables is fine.

    SQLITE_ERROR ("no such table") shares the DatabaseError family with the
    faults above. Reading it as a fault would make every fresh `kvseo init`
    exit 3.
    """
    db = unversioned_db(tmp_path / "fresh.db")

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
    sidecar = tmp_path / "precious-notes.txt-wal"
    sidecar.write_bytes(b"recovery data that exists independently of this call")

    with pytest.raises(DatabaseFileError) as exc:
        backup_to(source, dest)

    message = str(exc.value)
    assert str(dest) in message
    assert str(source) not in message, "blaming the source tells the user to delete a healthy database"
    assert "re-run `kvseo init`" not in message
    # The failure cleanup must not take the user's files with it: dest existed
    # before the call, so nothing at that path is ours to remove — a
    # pre-existing -wal is recovery data, not our scratch.
    assert dest.read_text(encoding="utf-8") == "the user's precious notes"
    assert sidecar.exists(), "cleanup deleted a pre-existing sidecar it never created"


def _magic_over_broken_header(path: Path) -> Path:
    """A source whose 16-byte magic is intact over a garbage page-size field.

    The shape that indicts code-based side attribution: it passes the magic
    guard, then raises SQLITE_NOTADB — the code the old handler read as "the
    destination's" — from the *source*. (An earlier version of this test used a
    plain text file, which the magic guard rejects before any copy runs; it
    pinned nothing about the paths it named.)
    """
    migrate(path)
    raw = bytearray(path.read_bytes())
    raw[16:18] = b"\x00\x07"
    path.write_bytes(bytes(raw))
    return path


def test_backup_to_blames_the_source_when_the_copy_reads_garbage(tmp_path: Path) -> None:
    """A broken source below the magic must be named as the source's fault.

    Reached via the pre-flight read: the magic guard cannot see past byte 15,
    so this source gets as far as ``backup_to``'s own reads. Called directly
    rather than through the CLI, which probes the source first and would never
    let this path run.
    """
    source = _magic_over_broken_header(tmp_path / "kvseo.db")
    dest = tmp_path / "backup.db"

    with pytest.raises(DatabaseFileError) as exc:
        backup_to(source, dest)

    message = str(exc.value)
    assert str(source) in message, "the failure is the source's, so the source is what to name"
    assert "could not write the backup" not in message


def test_backup_to_blames_the_destination_when_it_is_a_truncated_database(tmp_path: Path) -> None:
    """A truncated database at the destination must not indict a healthy source.

    Caught by the destination pre-flight (``BEGIN IMMEDIATE`` reads the header
    and the size-vs-page-count check fires), which is what makes the
    attribution positional rather than inferred. Under the old code-based
    inference this raised SQLITE_CORRUPT — read as "the source's" — and the
    advice was the destructive one: delete your live database. A truncated
    destination is exactly what a killed ``cp`` of an earlier backup leaves.
    """
    source = tmp_path / "kvseo.db"
    migrate(source)
    dest = tmp_path / "old-backup.db"
    migrate(dest)
    with dest.open("r+b") as fh:
        fh.truncate(dest.stat().st_size // 3)

    with pytest.raises(DatabaseFileError) as exc:
        backup_to(source, dest)

    message = str(exc.value)
    assert "could not write the backup" in message
    assert str(source) not in message, "blaming the source tells the user to delete a healthy database"
    assert "re-run `kvseo init`" not in message


def test_backup_to_reports_contention_instead_of_hanging(tmp_path: Path) -> None:
    """A lock the copy cannot get must become an error, not a silent hang.

    CPython's ``backup()`` retries a locked database forever — no cap, and the
    progress callback stays silent while it retries (measured) — so without the
    pre-flight read this test does not fail, it hangs. Needs a rollback-journal
    source: WAL gives readers a free pass, which is why `kvseo db backup` works
    against a live writer (pinned in test_db_commands).
    """
    source = tmp_path / "foreign.db"
    conn = sqlite3.connect(source)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.commit()
    conn.close()

    hog = sqlite3.connect(source, isolation_level=None)
    try:
        hog.execute("BEGIN EXCLUSIVE")

        with pytest.raises(DatabaseBusyError) as exc:
            backup_to(source, tmp_path / "backup.db")
    finally:
        hog.close()

    assert "in use by another process" in str(exc.value)


def test_blame_backup_side_probe_decides_the_side(tmp_path: Path) -> None:
    """The mid-copy attribution helper, pinned directly at both arms.

    No static file shape reaches the mid-copy branch any more — the pre-flight
    reads catch every enumerable fault before the copy starts (measured:
    truncation and header damage both die at pre-flight, on either side) — so
    the handler's remaining scope is the dynamic tail: IO errors, disk-full,
    corruption arriving mid-copy. Constructed exceptions are the deterministic
    way in, the same trade ``test_extended_result_codes_are_recognised`` makes.

    The codes are chosen adversarially: the healthy-source case carries
    SQLITE_CORRUPT (the code the old inference hardwired to the source) and
    must still blame the destination; the broken-source case carries
    SQLITE_NOTADB (hardwired to the destination) and must still blame the
    source. Only the probe, not the code, may decide.
    """
    from kvseo.storage.db import _blame_backup_side

    corrupt = sqlite3.DatabaseError("database disk image is malformed")
    corrupt.sqlite_errorcode = 11  # SQLITE_CORRUPT
    healthy = tmp_path / "healthy.db"
    migrate(healthy)
    conn = sqlite3.connect(healthy)
    try:
        with pytest.raises(DatabaseFileError) as exc:
            _blame_backup_side(healthy, tmp_path / "backup.db", conn, corrupt)
    finally:
        conn.close()
    assert "could not write the backup" in str(exc.value)
    assert str(healthy) not in str(exc.value), "a source that still answers reads is not the broken side"

    notadb = sqlite3.DatabaseError("file is not a database")
    notadb.sqlite_errorcode = 26  # SQLITE_NOTADB
    broken = _magic_over_broken_header(tmp_path / "broken.db")
    conn = sqlite3.connect(broken)  # lazy: the fault surfaces at the probe's read
    try:
        with pytest.raises(DatabaseFileError) as exc:
            _blame_backup_side(broken, tmp_path / "backup.db", conn, notadb)
    finally:
        conn.close()
    assert str(broken) in str(exc.value), "a source that cannot answer reads is the side to name"
    assert "could not write the backup" not in str(exc.value)


def test_remove_backup_artifacts_removes_only_what_the_copy_created(tmp_path: Path) -> None:
    """Cleanup semantics, pinned at unit level.

    Like the mid-copy handler above, the removal side is reachable end-to-end
    only through the dynamic tail (the pre-flights fail before the destination
    is created for every static fault), so the contract is pinned here: a
    destination the copy created is removed with its sidecars; a pre-existing
    destination keeps *everything* — its sidecars are recovery data, not our
    scratch. The precious-notes test pins the spare side end-to-end.
    """
    from kvseo.storage.db import _remove_backup_artifacts

    for name in ("fresh.db", "fresh.db-journal", "fresh.db-wal"):
        (tmp_path / name).write_bytes(b"partial wreckage")
    _remove_backup_artifacts(tmp_path / "fresh.db", spare_dest=False)
    assert list(tmp_path.glob("fresh.db*")) == [], "a created destination must be removed, sidecars included"

    for name in ("kept.db", "kept.db-wal"):
        (tmp_path / name).write_bytes(b"the user's recovery data")
    _remove_backup_artifacts(tmp_path / "kept.db", spare_dest=True)
    kept = sorted(p.name for p in tmp_path.glob("kept.db*"))
    assert kept == ["kept.db", "kept.db-wal"], "a pre-existing destination owns its sidecars too"


# --- Sub-header-size files: SQLite would initialise over them ---------------


def test_a_one_byte_file_is_refused_and_left_intact(bare_data_dir: Path) -> None:
    """SQLite treats a tiny file as an empty database and writes over it.

    Measured before the header check: a 1-byte file at the database path was
    replaced by a 144 KiB kvseo database, exit 0 — silent data loss on a file the
    user owns. 50 bytes and up already failed with SQLITE_NOTADB, so the gap was
    only below SQLite's own header threshold.
    """
    stray = bare_data_dir / "kvseo.db"
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
def test_cli_exits_3_on_a_file_that_is_not_a_database(bare_data_dir: Path, command: list[str]) -> None:
    _not_a_database(bare_data_dir / "kvseo.db")

    result = runner.invoke(app, command)

    assert result.exit_code == 3, result.output
    assert "not a usable kvseo database" in result.output


def test_version_and_help_survive_a_file_that_is_not_a_database(bare_data_dir: Path) -> None:
    """The refusal gates database work, not the whole CLI (as for the schema pin)."""
    _not_a_database(bare_data_dir / "kvseo.db")

    assert runner.invoke(app, ["--version"]).exit_code == 0
    assert runner.invoke(app, ["--help"]).exit_code == 0
