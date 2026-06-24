"""PSI connector: parsing, persistence, freshness, and the error cascade.

Network is mocked via httpx.MockTransport — no live PSI calls, no API key.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kvseo.connectors.base import (
    Connector,
    ConnectorAuthError,
    ConnectorRateLimited,
    ConnectorUnavailable,
)
from kvseo.connectors.psi import PsiConnector, PsiResult
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import PsiResult as PsiResultORM

FIXTURE = Path(__file__).parent.parent / "fixtures" / "psi" / "example_mobile.json"


def _payload() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _client(
    payload: Any, *, status: int = 200, headers: Mapping[str, str] | None = None
) -> httpx.AsyncClient:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload, headers=dict(headers or {}))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_parse_happy_path() -> None:
    conn = PsiConnector(client=_client(_payload()))
    res = await conn.core_web_vitals("https://example.com")

    assert isinstance(res, PsiResult)
    # Field data (CrUX); CLS percentile 8 → 0.08.
    assert res.field_lcp_ms == 2100
    assert res.field_inp_ms == 180
    assert res.field_cls == 0.08
    assert res.field_origin_fallback is False
    # Lab data (Lighthouse).
    assert res.lab_lcp_ms == 2450
    assert res.lab_tbt_ms == 120
    assert res.lab_cls == 0.05
    assert res.lab_performance_score == 86
    # Opportunities: the table-typed audit is excluded; sorted by savings desc.
    assert [o.id for o in res.opportunities] == [
        "render-blocking-resources",
        "unused-css-rules",
    ]
    assert res.opportunities[0].savings_ms == 450


async def test_conforms_to_connector_protocol() -> None:
    assert isinstance(PsiConnector(), Connector)


async def test_persists_one_row(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    conn = PsiConnector(engine=engine, client=_client(_payload()))
    await conn.core_web_vitals("https://example.com")
    with Session(engine) as s:
        count = s.scalar(select(func.count()).select_from(PsiResultORM))
        row = s.scalars(select(PsiResultORM)).one()
    assert count == 1
    assert row.lab_performance_score == 86
    assert [o["id"] for o in row.opportunities] == [
        "render-blocking-resources",
        "unused-css-rules",
    ]


async def test_freshness_serves_cache_without_refetch(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    await PsiConnector(engine=engine, client=_client(_payload())).core_web_vitals(
        "https://example.com"
    )
    # A second connector whose client would 500 must still succeed from cache.
    cached = await PsiConnector(
        engine=engine, client=_client({}, status=500)
    ).core_web_vitals("https://example.com")
    assert cached.url == "https://example.com"
    assert cached.lab_performance_score == 86
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(PsiResultORM)) == 1


async def test_refresh_bypasses_cache(tmp_path: Path) -> None:
    db = tmp_path / "kvseo.db"
    migrate(db)
    engine = get_engine(db)
    conn = PsiConnector(engine=engine, client=_client(_payload()))
    await conn.core_web_vitals("https://example.com")
    await PsiConnector(engine=engine, client=_client(_payload())).core_web_vitals(
        "https://example.com", refresh=True
    )
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(PsiResultORM)) == 2


async def test_auth_error_on_401() -> None:
    conn = PsiConnector(client=_client({}, status=401))
    with pytest.raises(ConnectorAuthError):
        await conn.core_web_vitals("https://example.com")


async def test_rate_limited_reads_retry_after() -> None:
    conn = PsiConnector(client=_client({}, status=429, headers={"Retry-After": "30"}))
    with pytest.raises(ConnectorRateLimited) as exc:
        await conn.core_web_vitals("https://example.com")
    assert exc.value.retry_after == 30


async def test_server_error_is_unavailable() -> None:
    conn = PsiConnector(client=_client({}, status=503))
    with pytest.raises(ConnectorUnavailable):
        await conn.core_web_vitals("https://example.com")


async def test_missing_lighthouse_degrades() -> None:
    conn = PsiConnector(client=_client({"loadingExperience": {}}))
    with pytest.raises(ConnectorUnavailable):
        await conn.core_web_vitals("https://example.com")
