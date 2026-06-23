"""L4 — models: UUID/JSON round-trips, server defaults, FK cascade."""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import AuditCheck, AuditRun


def test_audit_run_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    with Session(engine) as s:
        run = AuditRun(url="https://example.com")
        s.add(run)
        s.commit()
        run_id = run.id

    assert isinstance(run_id, uuid.UUID)  # PK is a UUID app-side, BLOB in the db
    with Session(engine) as s:
        loaded = s.get(AuditRun, run_id)
        assert loaded is not None
        assert loaded.url == "https://example.com"
        assert loaded.status == "running"  # server default applied
        assert loaded.strategy == "mobile"  # server default applied
        assert loaded.created_at  # timestamp auto-populated
        assert isinstance(loaded.id, uuid.UUID)


def test_json_column_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    payload = {"length": 55, "in_keyword_zone": True}
    with Session(engine) as s:
        run = AuditRun(url="https://example.com")
        s.add(run)
        s.flush()
        s.add(
            AuditCheck(
                audit_run_id=run.id,
                check_id="title.length",
                verdict="pass",
                severity="info",
                data=payload,
            )
        )
        s.commit()
        check_id = s.scalars(select(AuditCheck.id)).one()

    with Session(engine) as s:
        loaded = s.get(AuditCheck, check_id)
        assert loaded is not None
        assert loaded.data == payload


def test_cascade_delete_removes_checks(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    with Session(engine) as s:
        run = AuditRun(url="https://example.com")
        s.add(run)
        s.flush()
        s.add(
            AuditCheck(
                audit_run_id=run.id,
                check_id="title.length",
                verdict="pass",
                severity="info",
            )
        )
        s.commit()
        run_id = run.id

    # Deleting the parent cascades to children via ON DELETE CASCADE
    # (requires PRAGMA foreign_keys=ON, which get_engine sets per connection).
    with Session(engine) as s:
        run = s.get(AuditRun, run_id)
        assert run is not None
        s.delete(run)
        s.commit()

    with Session(engine) as s:
        remaining = s.scalar(select(func.count()).select_from(AuditCheck))
        assert remaining == 0
