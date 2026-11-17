"""CSV import connector — the manual escape hatch (03-connector-interfaces.md §7).

For users without API access, or migrating from another tool. A CSV is
normalized to the *same* pydantic model an API connector produces and written
to that connector's table — so downstream code (the advisor) can't tell a
CSV-imported row from a live-fetched one.

Three schemas, each written through its API connector's own write path:

* ``queries`` → :class:`GscQueryRow` → ``gsc_queries``: a Search Console export
  feeds the advisor exactly like the live GSC connector, without the OAuth dance.
* ``rankings`` → :class:`SerpBearRanking` → ``serpbear_rankings``: any rank
  tracker's export, read by the advisor like a SerpBear pull.
* ``keywords`` → :class:`DfsKeywordVolume` → ``dataforseo_data`` with
  ``source='csv'`` (03 §7): a Keyword Planner or third-party volume export.
  The source column keeps it from reading as DataForSEO's own data.

Import is transactional: per-row validation errors are collected, and if more
than ``max_failure_ratio`` of the rows fail (a wrong-file / wrong-schema
signal), nothing is committed. Otherwise the valid rows commit and the failures
are reported back in :class:`ImportResult`.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import Engine

from kvseo.connectors.base import ConnectorMeta
from kvseo.connectors.dataforseo import US, DfsKeywordVolume, persist_keyword_volumes
from kvseo.connectors.gsc import GscQueryRow, persist_gsc_rows
from kvseo.connectors.serpbear import SerpBearRanking, persist_rankings, serpbear_domain

ImportSchema = Literal["queries", "rankings", "keywords"]
_SUPPORTED: tuple[str, ...] = ("queries", "rankings", "keywords")
_TARGET_TABLE = {"queries": "gsc_queries", "rankings": "serpbear_rankings", "keywords": "dataforseo_data"}

# Header auto-mapping for the ``queries`` schema. Keys are our canonical
# GscQueryRow fields; values are header spellings we accept (lower-cased,
# stripped) from common exports — Google Search Console's UI export, the
# Search Analytics API, and a few third-party tools.
_QUERY_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    # "top queries" is the literal header of the GSC UI's Search-results export
    # — the exact file the no-API path exists for.
    "query": ("query", "queries", "top queries", "search query", "keyword", "keywords", "term"),
    "page": ("page", "pages", "landing page", "url", "address", "top pages"),
    "clicks": ("clicks", "url clicks", "click"),
    "impressions": ("impressions", "impr", "impr.", "impression"),
    "ctr": ("ctr", "url ctr", "click through rate", "click-through rate"),
    "position": ("position", "average position", "avg position", "avg. pos", "avg pos", "pos"),
}
_RANKING_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "keyword": ("keyword", "keywords", "query", "search term", "term"),
    "position": ("position", "rank", "ranking", "current position", "google position", "pos"),
    "url": ("url", "ranking url", "landing page", "page", "address"),
    "location": ("location", "country", "market"),
    "device": ("device",),
}
_INDEXED_COMPETITION = ("competition (indexed value)", "competition index")
_KEYWORD_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "keyword": ("keyword", "keywords", "query", "search term", "term"),
    "search_volume": ("search volume", "search_volume", "volume", "avg. monthly searches", "monthly searches", "sv"),
    "cpc": ("cpc", "cpc (usd)", "cost per click"),
    # Keyword Planner exports both; the indexed value (0-100) is the numeric one.
    "competition": (*_INDEXED_COMPETITION, "competition"),
}
_UNRANKED = {"", "0", "-", "—", "n/a", "not ranked", ">100", "100+", "> 100"}


class ImportRowError(BaseModel):
    """One rejected CSV row — 1-based line number plus why it was rejected."""

    row: int
    error: str


class ImportResult(BaseModel):
    """The outcome of a CSV import: what was read, what committed, what failed."""

    schema_name: str
    table: str
    path: str
    total_rows: int
    imported: int
    failed: int
    committed: bool
    errors: list[ImportRowError] = Field(default_factory=list)


class CsvImportError(Exception):
    """The CSV could not be imported at all (missing file, unmapped columns,
    unsupported schema). Distinct from per-row validation failures, which are
    collected into :class:`ImportResult` rather than raised."""


class CsvConnector:
    """Local CSV importer. No auth, no network — just a file and a mapping.

    ``engine`` enables persistence (the only useful mode); without it the
    connector validates and reports but writes nothing, which is handy for a
    dry run. ``max_failure_ratio`` is the share of rows that may fail validation
    before the whole import is treated as a wrong-file mistake and rolled back.
    """

    meta = ConnectorMeta(name="csv", version="0.2.0", capabilities=["queries", "rankings", "keywords"])

    def __init__(self, *, engine: Engine | None = None, max_failure_ratio: float = 0.5) -> None:
        self._engine = engine
        self._max_failure_ratio = max_failure_ratio

    async def health_check(self) -> bool:
        """CSV has nothing to reach — it's always 'connected'."""
        return True

    async def import_csv(
        self,
        path: Path,
        schema: ImportSchema = "queries",
        mapping: dict[str, str] | None = None,
        *,
        site: str,
        default_page: str | None = None,
        date_start: date | None = None,
        date_end: date | None = None,
    ) -> ImportResult:
        """Import a CSV into the table matching ``schema``.

        ``mapping`` maps our canonical field names to the CSV's header names and
        overrides auto-detection. ``site`` is the GSC property the rows belong to
        (``queries``) or the domain they rank for (``rankings``); keyword volume
        is market-level, so ``keywords`` ignores it. ``default_page`` fills the
        page column for query-only exports. ``date_start``/``date_end`` set the
        row date range for ``queries``; ``date_end`` dates a ``rankings`` import
        (default: today), since exports drop it.
        """
        if schema not in _SUPPORTED:
            raise CsvImportError(f"schema '{schema}' is not importable — use one of {_SUPPORTED}.")
        rows: Sequence[object]
        if schema == "queries":
            rows, errors, total = self._read_queries(
                path, mapping, default_page=default_page, date_start=date_start, date_end=date_end
            )
        elif schema == "rankings":
            checked = (date_end or _today()).isoformat()
            domain = serpbear_domain(site)
            if not domain:
                # Rows filed under no domain import "successfully" and are never read.
                raise CsvImportError(f"--site '{site}' names no domain for the rankings to belong to")
            rows, errors, total = self._read(
                path, mapping, _RANKING_HEADER_ALIASES, ("keyword", "position"),
                lambda raw, cols: _row_to_ranking(raw, cols, domain, checked),
            )
        else:
            fetched_at = datetime.now(UTC)
            rows, errors, total = self._read(
                path, mapping, _KEYWORD_HEADER_ALIASES, ("keyword", "search_volume"),
                lambda raw, cols: _row_to_volume(raw, cols, fetched_at),
            )

        committed = self._should_commit(len(errors), total)
        if committed and rows and self._engine is not None:
            if schema == "queries":
                persist_gsc_rows(self._engine, site, list(rows))  # type: ignore[arg-type]
            elif schema == "rankings":
                persist_rankings(self._engine, list(rows))  # type: ignore[arg-type]
            else:
                persist_keyword_volumes(self._engine, list(rows), cost=None, source="csv")  # type: ignore[arg-type]

        return ImportResult(
            schema_name=schema,
            table=_TARGET_TABLE[schema],
            path=str(path),
            total_rows=total,
            imported=len(rows) if committed else 0,
            failed=len(errors),
            committed=committed,
            errors=errors,
        )

    # --- Reading + validation --------------------------------------------

    def _read_queries(
        self,
        path: Path,
        mapping: dict[str, str] | None,
        *,
        default_page: str | None,
        date_start: date | None,
        date_end: date | None,
    ) -> tuple[list[GscQueryRow], list[ImportRowError], int]:
        if not path.exists():
            raise CsvImportError(f"CSV not found: {path}")
        # utf-8-sig transparently strips a UTF-8 BOM (Excel / GSC exports add one).
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise CsvImportError(f"CSV has no header row: {path}")
            colmap = self._resolve_columns(reader.fieldnames, mapping, default_page is not None)
            start = date_start or _today()
            end = date_end or _today()

            rows: list[GscQueryRow] = []
            errors: list[ImportRowError] = []
            total = 0
            for line_no, raw in enumerate(reader, start=2):  # line 1 is the header
                total += 1
                try:
                    rows.append(self._row_to_query(raw, colmap, default_page, start, end))
                except (ValueError, ValidationError) as exc:
                    errors.append(ImportRowError(row=line_no, error=_one_line(exc)))
        return rows, errors, total

    def _read[R](
        self,
        path: Path,
        mapping: dict[str, str] | None,
        aliases: dict[str, tuple[str, ...]],
        required: tuple[str, ...],
        to_row: Callable[[dict[str, str], dict[str, str]], R],
    ) -> tuple[list[R], list[ImportRowError], int]:
        """Read every row of ``path`` through ``to_row``, collecting per-row errors."""
        if not path.exists():
            raise CsvImportError(f"CSV not found: {path}")
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise CsvImportError(f"CSV has no header row: {path}")
            cols = _resolve(reader.fieldnames, mapping, aliases, required)
            rows: list[R] = []
            errors: list[ImportRowError] = []
            total = 0
            for line_no, raw in enumerate(reader, start=2):  # line 1 is the header
                total += 1
                try:
                    rows.append(to_row(raw, cols))
                except (ValueError, ValidationError) as exc:
                    errors.append(ImportRowError(row=line_no, error=_one_line(exc)))
        return rows, errors, total

    def _resolve_columns(
        self, fieldnames: Sequence[str], mapping: dict[str, str] | None, has_default_page: bool
    ) -> dict[str, str]:
        """Resolve each canonical field to an actual CSV header. Explicit
        ``mapping`` wins; otherwise match against the alias table. ``page`` is
        optional when a ``default_page`` was supplied."""
        present = {name.strip().lower(): name for name in fieldnames}
        resolved: dict[str, str] = {}
        for field_name, aliases in _QUERY_HEADER_ALIASES.items():
            if mapping and field_name in mapping:
                header = mapping[field_name]
                if header not in fieldnames:
                    raise CsvImportError(f"mapped column '{header}' for '{field_name}' not in CSV header")
                resolved[field_name] = header
                continue
            for alias in aliases:
                if alias in present:
                    resolved[field_name] = present[alias]
                    break

        missing = [f for f in _QUERY_HEADER_ALIASES if f not in resolved]
        if "page" in missing and has_default_page:
            missing.remove("page")
        if missing:
            raise CsvImportError(
                "could not map required column(s) "
                f"{missing} from header {fieldnames}. Pass --map field=header to map explicitly"
                + (" (or --page to supply a fixed page URL)." if "page" in missing else ".")
            )
        return resolved

    @staticmethod
    def _row_to_query(
        raw: dict[str, str],
        colmap: dict[str, str],
        default_page: str | None,
        start: date,
        end: date,
    ) -> GscQueryRow:
        query = (raw.get(colmap["query"]) or "").strip()
        if not query:
            raise ValueError("empty query")
        page_cell = raw.get(colmap["page"]) if "page" in colmap else None
        page = (page_cell or "").strip() or (default_page or "")
        if not page:
            raise ValueError("no page (column empty and no --page default)")
        return GscQueryRow(
            query=query,
            page=page,
            clicks=_int(raw.get(colmap["clicks"])),
            impressions=_int(raw.get(colmap["impressions"])),
            ctr=_ctr(raw.get(colmap["ctr"])),
            position=_float(raw.get(colmap["position"])),
            date_range_start=start,
            date_range_end=end,
        )

    def _should_commit(self, failed: int, total: int) -> bool:
        if total == 0:
            return False
        return (failed / total) <= self._max_failure_ratio



def _resolve(
    fieldnames: Sequence[str],
    mapping: dict[str, str] | None,
    aliases: dict[str, tuple[str, ...]],
    required: tuple[str, ...],
) -> dict[str, str]:
    """Map canonical fields to CSV headers: an explicit ``mapping`` wins, then the
    first alias present. Optional fields may stay unmapped."""
    present = {name.strip().lower(): name for name in fieldnames}
    resolved: dict[str, str] = {}
    for field_name, names in aliases.items():
        if mapping and field_name in mapping:
            if mapping[field_name] not in fieldnames:
                raise CsvImportError(f"mapped column '{mapping[field_name]}' for '{field_name}' not in CSV header")
            resolved[field_name] = mapping[field_name]
            continue
        found = next((present[a] for a in names if a in present), None)
        if found is not None:
            resolved[field_name] = found
    missing = [f for f in required if f not in resolved]
    if missing:
        raise CsvImportError(
            f"could not map required column(s) {missing} from header {list(fieldnames)}. "
            "Pass --map field=header to map explicitly."
        )
    return resolved


def _cell(raw: dict[str, str], cols: dict[str, str], name: str) -> str:
    return (raw.get(cols[name]) or "").strip() if name in cols else ""


def _row_to_ranking(raw: dict[str, str], cols: dict[str, str], domain: str, checked: str) -> SerpBearRanking:
    keyword = _cell(raw, cols, "keyword")
    if not keyword:
        raise ValueError("empty keyword")
    position_cell = _cell(raw, cols, "position")
    position = None if position_cell.lower() in _UNRANKED else int(float(position_cell))
    if position is not None and position < 1:
        raise ValueError(f"position {position} is below 1")
    return SerpBearRanking(
        keyword=keyword,
        domain=domain,
        location=_cell(raw, cols, "location") or None,
        device=_cell(raw, cols, "device").lower() or None,
        position=position,
        url=_cell(raw, cols, "url") or None,
        checked_at=checked,
        history=[],
    )


def _row_to_volume(raw: dict[str, str], cols: dict[str, str], fetched_at: datetime) -> DfsKeywordVolume:
    keyword = _cell(raw, cols, "keyword")
    if not keyword:
        raise ValueError("empty keyword")
    volume = _cell(raw, cols, "search_volume")
    cpc = _cell(raw, cols, "cpc").lstrip("$")
    return DfsKeywordVolume(
        keyword=keyword,
        location_code=US,
        language_code="en",
        search_volume=_int(volume) if volume else None,
        cpc_usd=_float(cpc) if cpc else None,
        competition=_competition(
            _cell(raw, cols, "competition"),
            indexed=cols.get("competition", "").strip().lower() in _INDEXED_COMPETITION,
        ),
        monthly_searches=[],
        fetched_at=fetched_at,
    )


def _competition(value: str, *, indexed: bool) -> float | None:
    """A 0-1 fraction. An indexed column is 0-100 on every row, so an index of 1
    is 0.01; only an unnamed scale is guessed from the number itself. Text levels
    ('Low', 'High') carry no number to keep, so they read as unknown."""
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    return number / 100 if indexed or number > 1 else number


def _today() -> date:
    return datetime.now(UTC).date()


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())


def _int(value: str | None) -> int:
    if value is None or not value.strip():
        return 0
    return int(float(value.replace(",", "").strip()))


def _float(value: str | None) -> float:
    if value is None or not value.strip():
        return 0.0
    return float(value.replace(",", "").strip())


def _ctr(value: str | None) -> float:
    """Parse a CTR cell to a 0.0-1.0 fraction. Accepts '12.3%' (-> 0.123) and a
    bare fraction '0.123'. A bare number > 1 is read as a percentage point."""
    if value is None or not value.strip():
        return 0.0
    text = value.strip()
    if text.endswith("%"):
        return float(text[:-1].strip()) / 100
    number = float(text.replace(",", ""))
    return number / 100 if number > 1 else number
