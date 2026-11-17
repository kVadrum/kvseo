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
from kvseo.connectors.dataforseo import latest_keyword_volume
from kvseo.connectors.serpbear import rankings_for_page
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import DataForSeoData
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


async def test_gsc_ui_export_header_automaps(tmp_path: Path) -> None:
    # The literal header of the GSC UI's Search-results export ("Top queries")
    # must auto-map — it is the exact file the no-API path exists for. Caught
    # missing in the v0.9.5 pre-tag wheel test; do not let it regress.
    engine = _engine(tmp_path)
    conn = CsvConnector(engine=engine)
    result = await conn.import_csv(FIXTURES / "queries_gsc_ui.csv", site=SITE, default_page=SITE)
    assert result.committed is True
    assert (result.total_rows, result.imported, result.failed) == (2, 2, 0)
    with Session(engine) as s:
        top = s.scalars(select(GscQueryORM).order_by(GscQueryORM.impressions.desc())).first()
    assert top is not None
    assert top.query == "best local fixtures"
    assert top.ctr == pytest.approx(0.08)
    assert top.position == pytest.approx(8.2)


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
        await conn.import_csv(FIXTURES / "queries_happy.csv", schema="pages", site=SITE)  # type: ignore[arg-type]


async def test_missing_file_raises(tmp_path: Path) -> None:
    conn = CsvConnector(engine=_engine(tmp_path))
    with pytest.raises(CsvImportError, match="not found"):
        await conn.import_csv(tmp_path / "nope.csv", site=SITE)


async def test_rankings_import_reads_like_a_serpbear_pull(tmp_path: Path) -> None:
    """A generic rank-tracker export lands in serpbear_rankings through the same
    write path, so the advisor's page lookup finds it."""
    csv_path = tmp_path / "ranks.csv"
    csv_path.write_text(
        "Keyword,Rank,Ranking URL,Country\n"
        "ops consulting,14,https://www.kemek.net/services/,US\n"
        "kemek network,1,https://kemek.net/,US\n"
        "west virginia ops,>100,,US\n"
        ",3,https://kemek.net/x,US\n",
        encoding="utf-8",
    )
    engine = _engine(tmp_path)
    result = await CsvConnector(engine=engine).import_csv(csv_path, schema="rankings", site="https://kemek.net/")

    assert (result.table, result.imported, result.failed) == ("serpbear_rankings", 3, 1)
    with Session(engine) as session:
        on_page = rankings_for_page(session, "https://kemek.net/services", None, limit=25)
        assert [(r.keyword, r.position, r.domain) for r in on_page] == [("ops consulting", 14, "kemek.net")]
        unranked = rankings_for_page(session, "https://kemek.net/services", "west virginia ops", limit=25)
        assert ("west virginia ops", None) in [(r.keyword, r.position) for r in unranked]


async def test_keywords_import_is_marked_csv_not_dataforseo(tmp_path: Path) -> None:
    """A Keyword Planner export: the indexed competition column wins over the
    text one, and the rows say they came from a CSV."""
    csv_path = tmp_path / "planner.csv"
    csv_path.write_text(
        "Keyword,Avg. monthly searches,Competition,Competition (indexed value),CPC (USD)\n"
        "ops consulting,880,Low,23,$9.50\n"
        "seo audit,\"2,900\",High,71,6.1\n"
        "ops audit template,40,Low,1,0.80\n",
        encoding="utf-8",
    )
    engine = _engine(tmp_path)
    result = await CsvConnector(engine=engine).import_csv(csv_path, schema="keywords", site=SITE)

    assert (result.table, result.imported) == ("dataforseo_data", 3)
    with Session(engine) as session:
        stored = latest_keyword_volume(session, "seo audit")
        assert stored is not None
        volume, source, _ = stored
        assert (volume.search_volume, volume.competition, volume.cpc_usd, source) == (2900, 0.71, 6.1, "csv")
        # An index of 1 is the bottom of the 0-100 scale, not 100%.
        low = latest_keyword_volume(session, "ops audit template")
        assert low is not None and low[0].competition == 0.01
        costs = session.scalars(select(DataForSeoData.estimated_cost_usd)).all()
        assert costs == [None, None, None]  # nothing was billed


@pytest.mark.parametrize("site", ["sc-domain:kemek.net", "kemek.net", "www.kemek.net"])
async def test_rankings_import_accepts_a_domain_property_or_bare_host(tmp_path: Path, site: str) -> None:
    """--site takes a GSC domain property for `queries`; the same value must file
    rankings under the domain the page lookup reads, not under no domain."""
    csv_path = tmp_path / "ranks.csv"
    csv_path.write_text("Keyword,Rank,URL\nops consulting,14,https://kemek.net/services/\n", encoding="utf-8")
    engine = _engine(tmp_path)
    await CsvConnector(engine=engine).import_csv(csv_path, schema="rankings", site=site)
    with Session(engine) as session:
        found = rankings_for_page(session, "https://kemek.net/services", None, limit=25)
        assert [(r.keyword, r.domain) for r in found] == [("ops consulting", "kemek.net")]


async def test_rankings_import_refuses_a_site_with_no_domain(tmp_path: Path) -> None:
    csv_path = tmp_path / "ranks.csv"
    csv_path.write_text("Keyword,Rank\nops consulting,14\n", encoding="utf-8")
    conn = CsvConnector(engine=_engine(tmp_path))
    with pytest.raises(CsvImportError, match="names no domain"):
        await conn.import_csv(csv_path, schema="rankings", site="sc-domain:")
    with pytest.raises(CsvImportError, match="--site is required"):
        await conn.import_csv(csv_path, schema="rankings", site=" ")
