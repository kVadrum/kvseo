"""``kvseo db migrate | backup | vacuum`` — 06 §4.10, 07 §5.

The three operator commands over the SQLite file. What's worth pinning here is
mostly the *boundaries*: backup must not migrate (07 §5's whole point is a copy
of the file as it stands), must not clobber, and must survive a database this
build considers too new — that last one being exactly when a copy matters most.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.cli import db as cli_db
from kvseo.storage.db import HEAD_REVISION, get_engine, stored_revision

runner = CliRunner()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A configured, initialised kvseo data directory."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    assert runner.invoke(app, ["init"]).exit_code == 0
    return data


def _set_revision(db: Path, revision: str) -> None:
    engine = get_engine(db)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = :r"), {"r": revision})
    engine.dispose()


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# --- db migrate ------------------------------------------------------------


def test_migrate_reports_an_already_current_database(data_dir: Path) -> None:
    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert "already at schema" in result.output
    assert HEAD_REVISION in result.output


def test_migrate_brings_an_unmigrated_database_to_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid SQLite file with none of our tables is the "unversioned" case."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    db = data / "kvseo.db"
    engine = get_engine(db)
    engine.connect().close()
    engine.dispose()
    assert stored_revision(db) is None

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 0, result.output
    assert "(unversioned)" in result.output and HEAD_REVISION in result.output
    assert stored_revision(db) == HEAD_REVISION
    assert "audit_runs" in _tables(db)


def test_migrate_refuses_a_database_newer_than_the_package(data_dir: Path) -> None:
    """07 §4's pin still applies — migrating forward cannot fix an ahead database."""
    _set_revision(data_dir / "kvseo.db", "9999")

    result = runner.invoke(app, ["db", "migrate"])

    assert result.exit_code == 3
    assert "9999" in result.output


# --- db backup -------------------------------------------------------------


def test_backup_writes_a_timestamped_copy(data_dir: Path) -> None:
    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 0, result.output
    backups = list((data_dir / "backups").glob("kvseo-*.db"))
    assert len(backups) == 1
    assert backups[0].name.startswith("kvseo-")
    assert str(backups[0]) in result.output


def test_backup_copy_is_a_usable_database(data_dir: Path) -> None:
    """A backup nobody can read is not a backup — check the schema came across."""
    assert runner.invoke(app, ["db", "backup"]).exit_code == 0

    copy = next((data_dir / "backups").glob("kvseo-*.db"))
    assert stored_revision(copy) == HEAD_REVISION
    assert "audit_runs" in _tables(copy)


def test_backup_honours_output(data_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "elsewhere" / "snapshot.db"

    result = runner.invoke(app, ["db", "backup", "--output", str(dest)])

    assert result.exit_code == 0, result.output
    assert stored_revision(dest) == HEAD_REVISION
    assert not (data_dir / "backups").exists()


def test_backup_refuses_to_overwrite(data_dir: Path, tmp_path: Path) -> None:
    dest = tmp_path / "taken.db"
    dest.write_text("something the user cares about", encoding="utf-8")

    result = runner.invoke(app, ["db", "backup", "--output", str(dest)])

    assert result.exit_code == 2
    assert "refusing to overwrite" in result.output
    assert dest.read_text(encoding="utf-8") == "something the user cares about"


def test_two_default_backups_in_the_same_second_both_land(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default name is ours, so a same-second collision is ours to absorb.

    The clock is frozen rather than raced: two real invocations usually land in
    different seconds, so a timing-dependent version of this test would pass
    without ever exercising the collision it exists to pin. An explicit --output
    keeps the strict refusal (see the test above) — only the generated name
    falls back to a suffix.
    """
    monkeypatch.setattr(cli_db, "_BACKUP_TS", "frozen")

    assert runner.invoke(app, ["db", "backup"]).exit_code == 0
    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 0, result.output
    copies = sorted(p.name for p in (data_dir / "backups").glob("kvseo-*.db"))
    assert copies == ["kvseo-frozen-2.db", "kvseo-frozen.db"]
    for name in copies:
        assert stored_revision(data_dir / "backups" / name) == HEAD_REVISION


def test_backup_blames_the_output_path_not_the_source(data_dir: Path, tmp_path: Path) -> None:
    """A destination that cannot be written must not indict a healthy source.

    The failing code (SQLITE_CANTOPEN) is the same one a missing source raises,
    so attributing it by position rather than by path sent the user to fix
    $KVSEO_DATA_DIR when the actual problem was the --output they just passed.
    """
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir()
    unwritable.chmod(0o500)
    try:
        result = runner.invoke(app, ["db", "backup", "--output", str(unwritable / "snapshot.db")])
    finally:
        unwritable.chmod(0o700)

    assert result.exit_code == 3
    assert "could not write the backup" in result.output
    assert "snapshot.db" in result.output
    assert str(data_dir / "kvseo.db") not in result.output
    assert "KVSEO_DATA_DIR" not in result.output


def test_backup_output_under_a_regular_file_is_a_usage_error(data_dir: Path, tmp_path: Path) -> None:
    """`exist_ok=True` tolerates an existing directory, not an existing file."""
    not_a_dir = tmp_path / "a-plain-file"
    not_a_dir.write_text("this is a file, not a directory", encoding="utf-8")

    result = runner.invoke(app, ["db", "backup", "--output", str(not_a_dir / "snapshot.db")])

    assert result.exit_code == 2
    assert "cannot create the directory" in result.output
    assert not_a_dir.read_text(encoding="utf-8") == "this is a file, not a directory"


def test_backup_does_not_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """07 §5: the copy is of the file as it stands, not of a file we changed first."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    db = data / "kvseo.db"
    engine = get_engine(db)
    engine.connect().close()
    engine.dispose()

    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 0, result.output
    assert "unversioned" in result.output
    assert stored_revision(db) is None, "backup migrated the source database"


def test_backup_works_on_a_database_newer_than_the_package(data_dir: Path) -> None:
    """The one case where a copy matters most must not be the one that refuses."""
    _set_revision(data_dir / "kvseo.db", "9999")

    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 0, result.output
    assert "9999" in result.output
    copy = next((data_dir / "backups").glob("kvseo-*.db"))
    assert stored_revision(copy) == "9999"


def test_backup_without_a_database_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))

    result = runner.invoke(app, ["db", "backup"])

    assert result.exit_code == 3
    assert "kvseo init" in result.output


# --- db vacuum -------------------------------------------------------------


def test_vacuum_succeeds_and_preserves_the_schema(data_dir: Path) -> None:
    result = runner.invoke(app, ["db", "vacuum"])

    assert result.exit_code == 0, result.output
    assert "vacuumed database" in result.output
    db = data_dir / "kvseo.db"
    assert stored_revision(db) == HEAD_REVISION
    assert "audit_runs" in _tables(db)


def test_vacuum_reclaims_space_after_a_delete(data_dir: Path) -> None:
    """The size report has to reflect reality, so give it real free pages."""
    db = data_dir / "kvseo.db"
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        conn.execute("CREATE TABLE bulk (blob TEXT)")
        conn.executemany("INSERT INTO bulk VALUES (?)", [("x" * 2000,) for _ in range(2000)])
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("DROP TABLE bulk")
    finally:
        conn.close()
    before = db.stat().st_size

    result = runner.invoke(app, ["db", "vacuum"])

    assert result.exit_code == 0, result.output
    assert "reclaimed" in result.output
    assert db.stat().st_size < before


def test_vacuum_reports_lock_contention_instead_of_raising(data_dir: Path) -> None:
    """A concurrent writer is ordinary, so it must not surface as a traceback.

    VACUUM is the only command here that needs exclusive access — backup,
    migrate and the query commands all work against a live writer. Exit 1 per
    06 §2 ("general error, caught exception"); the table has no code for lock
    contention and says not to invent one.
    """
    hog = sqlite3.connect(data_dir / "kvseo.db", isolation_level=None)
    try:
        hog.execute("BEGIN IMMEDIATE")
        hog.execute("INSERT INTO sites (id, origin) VALUES (?, 'holder.example')", (uuid.uuid4().bytes,))

        result = runner.invoke(app, ["db", "vacuum"])

        assert result.exit_code == 1
        assert "in use by another process" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
    finally:
        hog.execute("ROLLBACK")
        hog.close()


def test_backup_survives_a_concurrent_writer(data_dir: Path) -> None:
    """07 §5's claim: safe to run with the database in use, unlike a plain cp."""
    hog = sqlite3.connect(data_dir / "kvseo.db", isolation_level=None)
    try:
        hog.execute("BEGIN IMMEDIATE")
        hog.execute("INSERT INTO sites (id, origin) VALUES (?, 'holder.example')", (uuid.uuid4().bytes,))

        result = runner.invoke(app, ["db", "backup"])

        assert result.exit_code == 0, result.output
    finally:
        hog.execute("ROLLBACK")
        hog.close()

    copy = next((data_dir / "backups").glob("kvseo-*.db"))
    assert stored_revision(copy) == HEAD_REVISION


def test_vacuum_refuses_rather_than_underreporting_under_a_reader(data_dir: Path) -> None:
    """A concurrent *reader* must not produce "nothing to reclaim" after reclaiming.

    VACUUM succeeds against a reader, but the rebuilt database lands in the WAL
    and nothing can checkpoint it into the main file until that reader leaves —
    so the size comparison saw before == after and reported no gain on a command
    whose entire purpose is the gain. The opening checkpoint reports busy instead
    of raising, which is what makes this detectable.
    """
    db = data_dir / "kvseo.db"
    writer = sqlite3.connect(db, isolation_level=None)
    writer.execute("CREATE TABLE bulk (blob TEXT)")
    writer.executemany("INSERT INTO bulk VALUES (?)", [("x" * 2000,) for _ in range(1500)])
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("DROP TABLE bulk")
    writer.close()
    fat = db.stat().st_size

    reader = sqlite3.connect(db)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM sites").fetchone()

        result = runner.invoke(app, ["db", "vacuum"])
    finally:
        reader.close()

    assert result.exit_code == 1
    assert "in use by another process" in result.output
    assert "nothing to reclaim" not in result.output
    # Once the reader lets go the command works and the space really was there.
    assert runner.invoke(app, ["db", "vacuum"]).exit_code == 0
    assert db.stat().st_size < fat


def test_vacuum_refuses_a_database_newer_than_the_package(data_dir: Path) -> None:
    """Don't rebuild a file written to a schema this build cannot describe."""
    _set_revision(data_dir / "kvseo.db", "9999")

    result = runner.invoke(app, ["db", "vacuum"])

    assert result.exit_code == 3
    assert "9999" in result.output


# --- contention on the migrate path (every command opens this way) ---------


@pytest.mark.parametrize("command", [["init"], ["db", "migrate"], ["cost"]])
def test_pending_migrations_under_a_writer_report_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: list[str]
) -> None:
    """Contention needs BOTH a writer and migrations actually pending.

    v0.10.1 scoped the busy path to `db vacuum` on a measurement taken against a
    database already at head, where migrate is a no-op read that contends with
    nothing. With DDL to run it contends like anything else — and migrate is on
    every command's path, not just a monthly maintenance one. Two ordinary
    triggers: a first `init` over a pre-existing unversioned file, and the
    migrate-on-open after any upgrade that ships a migration.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KVSEO_DATA_DIR", str(data))
    monkeypatch.setenv("KVSEO_CONFIG_DIR", str(tmp_path / "cfg"))
    db = data / "kvseo.db"
    # A valid SQLite file with no alembic_version — migrate has real DDL to run.
    seed = sqlite3.connect(db)
    seed.execute("CREATE TABLE decoy (x)")
    seed.commit()
    seed.close()

    writer = sqlite3.connect(db, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO decoy VALUES (1)")

        result = runner.invoke(app, command)
    finally:
        writer.execute("ROLLBACK")
        writer.close()

    assert result.exit_code == 1, result.output
    assert "in use by another process" in result.output
    assert "applying migrations" in result.output


# --- group wiring ----------------------------------------------------------


def test_db_group_lists_its_subcommands() -> None:
    result = runner.invoke(app, ["db", "--help"])

    assert result.exit_code == 0
    for subcommand in ("migrate", "backup", "vacuum"):
        assert subcommand in result.output


def test_bare_db_shows_help_rather_than_acting() -> None:
    """no_args_is_help: a typo'd `kvseo db` must not silently do something."""
    result = runner.invoke(app, ["db"])

    assert result.exit_code != 0
    assert "Usage" in result.output
