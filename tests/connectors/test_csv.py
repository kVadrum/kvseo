"""CSV connector: header mapping, per-row validation, transactional rollback.

No network, no auth — the connector reads a local file. Persistence is verified
against a real (temp) SQLite database.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.connectors.base import Connector
from kvseo.connectors.csv import CsvConnector, CsvImportError
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import GscQuery as GscQueryORM

FIXTURES = Path(__file__).parent.parent / "fixtures" / "csv"
SITE = "https://kemek.net/"


def _engine(tmp_path: Path):  # type: ignore[no-untyped-def]
    db = tmp_path / "kvseo.db"
    migrate(db)
    return get_engine(db)


async def test_conforms_to_connector_protocol() -> None:
    assert isinstance(CsvConnector(), Connector)


async def test_happy_path_imports_and_persists(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(
        FIXTURES / "queries_happy.csv", site=SITE, date_start=date(2026, 5, 1), date_end=date(2026, 5, 31)
    )

    assert result.committed is True
    assert (result.total_rows, result.imported, result.failed) == (3, 3, 0)
    with Session(engine) as s:
        rows = s.scalars(select(GscQueryORM).order_by(GscQueryORM.impressions.desc())).all()
    assert len(rows) == 3
    top = rows[0]
    assert top.query == "ops consulting west virginia"
    assert top.page == "https://kemek.net/services"
    assert top.clicks == 8
    assert top.impressions == 412
    assert top.ctr == pytest.approx(0.019)  # '1.9%' → 0.019
    assert top.position == pytest.approx(14.2)
    assert top.range_start == "2026-05-01"
    assert top.site_origin == SITE


async def test_bom_and_default_page(tmp_path: Path) -> None:
    # A query-only export (no page column) with a UTF-8 BOM: the BOM must be
    # stripped from the first header, and --page fills the missing column.
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(
        FIXTURES / "queries_bom_nopage.csv", site=SITE, default_page="https://kemek.net/"
    )
    assert result.committed is True
    assert result.imported == 2
    with Session(engine) as s:
        pages = {r.page for r in s.scalars(select(GscQueryORM)).all()}
    assert pages == {"https://kemek.net/"}


async def test_partial_failure_commits_valid_rows(tmp_path: Path) -> None:
    # One of four rows has an empty query → 25% fail, under the 50% threshold,
    # so the three valid rows commit and the failure is reported.
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(FIXTURES / "queries_partial.csv", site=SITE)

    assert result.committed is True
    assert (result.total_rows, result.imported, result.failed) == (4, 3, 1)
    assert result.errors[0].row == 3  # 1=header, 2=first data row, 3=the empty-query row
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(GscQueryORM)) == 3


async def test_too_many_failures_rolls_back(tmp_path: Path) -> None:
    # Three of four rows are invalid → over threshold → nothing commits.
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(FIXTURES / "queries_mostly_bad.csv", site=SITE)

    assert result.committed is False
    assert result.imported == 0
    assert result.failed == 3
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(GscQueryORM)) == 0


async def test_out_of_range_values_become_row_errors(tmp_path: Path) -> None:
    # A CTR > 100% and a negative click count must be rejected per-row (the
    # GscQueryRow bounds), not persisted as poisoned advisor evidence. The three
    # valid rows still commit (2/5 fail, under the 50% threshold).
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(FIXTURES / "queries_outofrange.csv", site=SITE)

    assert result.committed is True
    assert (result.total_rows, result.imported, result.failed) == (5, 3, 2)
    assert {e.row for e in result.errors} == {2, 3}  # the ctr-over-100 and negative-clicks rows
    with Session(engine) as s:
        rows = s.scalars(select(GscQueryORM)).all()
    assert len(rows) == 3
    assert all(0.0 <= r.ctr <= 1.0 and r.clicks >= 0 for r in rows)


async def test_unmappable_header_raises(tmp_path: Path) -> None:
    conn = CsvConnector(engine=_engine(tmp_path))
    with pytest.raises(CsvImportError, match="could not map"):
        await conn.import_csv(FIXTURES / "queries_mismatch.csv", site=SITE)


async def test_explicit_mapping_overrides_autodetect(tmp_path: Path) -> None:
    # Fully non-standard headers that auto-detection can't resolve; an explicit
    # field=header mapping wires every required column through.
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(
        FIXTURES / "queries_renamed.csv",
        site=SITE,
        mapping={
            "query": "kw",
            "page": "addr",
            "clicks": "hits",
            "impressions": "views",
            "ctr": "rate",
            "position": "rank",
        },
    )
    assert result.committed is True
    assert result.imported == 2
    with Session(engine) as s:
        row = s.scalars(select(GscQueryORM).order_by(GscQueryORM.impressions.desc())).first()
    assert row is not None
    assert row.query == "ops consulting"
    assert row.ctr == pytest.approx(0.019)


async def test_bad_mapping_target_raises(tmp_path: Path) -> None:
    conn = CsvConnector(engine=_engine(tmp_path))
    with pytest.raises(CsvImportError, match="not in CSV header"):
        await conn.import_csv(
            FIXTURES / "queries_happy.csv", site=SITE, mapping={"query": "DoesNotExist"}
        )


async def test_unsupported_schema_rejected(tmp_path: Path) -> None:
    conn = CsvConnector(engine=_engine(tmp_path))
    with pytest.raises(CsvImportError, match="not importable"):
        await conn.import_csv(FIXTURES / "queries_happy.csv", schema="keywords", site=SITE)  # type: ignore[arg-type]


async def test_missing_file_raises(tmp_path: Path) -> None:
    conn = CsvConnector(engine=_engine(tmp_path))
    with pytest.raises(CsvImportError, match="not found"):
        await conn.import_csv(tmp_path / "nope.csv", site=SITE)
