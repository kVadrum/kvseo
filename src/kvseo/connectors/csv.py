"""CSV import connector — the manual escape hatch (03-connector-interfaces.md §7).

For users without API access, or migrating from another tool. A CSV is
normalized to the *same* pydantic model an API connector produces and written
to that connector's table — so downstream code (the advisor) can't tell a
CSV-imported row from a live-fetched one.

v0.1 supports one schema, ``queries`` → :class:`GscQueryRow` → ``gsc_queries``.
That is the load-bearing escape hatch: a Search-Console export feeds the advisor
exactly like the live GSC connector does, without the OAuth dance. The
``keywords`` and ``rankings`` schemas in the spec target the DataForSEO /
SerpBear tables, which land with those connectors in v0.2 (03 §4-6); they are
deliberately not wired here yet — ``import_csv`` rejects them with a clear error
rather than pretending. Adding them in v0.2 is purely a new branch + target
table.

Import is transactional: per-row validation errors are collected, and if more
than ``max_failure_ratio`` of the rows fail (a wrong-file / wrong-schema
signal), nothing is committed. Otherwise the valid rows commit and the failures
are reported back in :class:`ImportResult`.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.connectors.base import ConnectorMeta
from kvseo.connectors.gsc import GscQueryRow
from kvseo.storage.models import GscQuery as GscQueryORM

ImportSchema = Literal["queries"]
_SUPPORTED: tuple[str, ...] = ("queries",)
_SQLITE_TS = "%Y-%m-%d %H:%M:%S"

# Header auto-mapping for the ``queries`` schema. Keys are our canonical
# GscQueryRow fields; values are header spellings we accept (lower-cased,
# stripped) from common exports — Google Search Console's UI export, the
# Search Analytics API, and a few third-party tools.
_QUERY_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "query": ("query", "queries", "search query", "keyword", "keywords", "term"),
    "page": ("page", "pages", "landing page", "url", "address", "top pages"),
    "clicks": ("clicks", "url clicks", "click"),
    "impressions": ("impressions", "impr", "impr.", "impression"),
    "ctr": ("ctr", "url ctr", "click through rate", "click-through rate"),
    "position": ("position", "average position", "avg position", "avg. pos", "avg pos", "pos"),
}


class ImportRowError(BaseModel):
    """One rejected CSV row — 1-based line number plus why it was rejected."""

    row: int
    error: str


class ImportResult(BaseModel):
    """The outcome of a CSV import: what was read, what committed, what failed."""

    schema_name: str
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

    meta = ConnectorMeta(name="csv", version="0.1.0", capabilities=["queries"])

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
        (``gsc_queries.site_origin``). ``default_page`` fills the page column for
        query-only exports that don't carry a page. ``date_start``/``date_end``
        set the row date range (default: today), since UI exports drop it.
        """
        if schema not in _SUPPORTED:
            raise CsvImportError(
                f"schema '{schema}' is not importable in v0.1 — only {_SUPPORTED} is "
                "supported. The 'keywords' and 'rankings' schemas land with the "
                "DataForSEO / SerpBear connectors in v0.2."
            )
        rows, errors, total = self._read_queries(
            path, mapping, default_page=default_page, date_start=date_start, date_end=date_end
        )

        committed = self._should_commit(len(errors), total)
        if committed and rows:
            self._persist(site, rows)

        return ImportResult(
            schema_name=schema,
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

    # --- Persistence ------------------------------------------------------

    def _persist(self, site: str, rows: list[GscQueryRow]) -> None:
        if self._engine is None:
            return
        # One timestamp for the whole batch, matching GscConnector._persist so a
        # freshness read picks up the entire import, not a sub-second slice.
        fetched_at = datetime.now(UTC).strftime(_SQLITE_TS)
        with Session(self._engine) as session:
            session.add_all(
                GscQueryORM(
                    site_origin=site,
                    page=r.page,
                    query=r.query,
                    clicks=r.clicks,
                    impressions=r.impressions,
                    ctr=r.ctr,
                    position=r.position,
                    range_start=r.date_range_start.isoformat(),
                    range_end=r.date_range_end.isoformat(),
                    fetched_at=fetched_at,
                )
                for r in rows
            )
            session.commit()


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
