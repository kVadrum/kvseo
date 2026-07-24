"""`kvseo cost` and the cost roll-up over stored advisor runs (06 §4.9)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from kvseo.cli import app
from kvseo.core.cost import summarize_cost
from kvseo.storage.db import get_engine, migrate
from kvseo.storage.models import AdvisorOutput

runner = CliRunner()


def _add_run(
    engine: Engine,
    audit_id: uuid.UUID,
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-5",
    prompt_id: str = "prioritize",
    cost: float | None = 0.10,
    created_at: str = "2026-06-15 12:00:00",
    status: str = "success",
) -> None:
    with Session(engine) as s:
        s.add(
            AdvisorOutput(
                audit_run_id=audit_id,
                prompt_id=prompt_id,
                provider=provider,
                model=model,
                status=status,
                estimated_cost_usd=cost,
                created_at=created_at,
            )
        )
        s.commit()


def test_summarize_totals_and_provider_breakdown(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    _add_run(audit_engine, aid, provider="anthropic", cost=0.30, prompt_id="prioritize")
    _add_run(audit_engine, aid, provider="openai", cost=0.10, prompt_id="report")
    _add_run(audit_engine, aid, provider="anthropic", cost=0.05, prompt_id="prioritize")

    summary = summarize_cost(audit_engine, by="provider")

    assert summary.runs == 3
    assert summary.total_usd == pytest.approx(0.45)
    # Descending by cost: anthropic (0.35) before openai (0.10).
    assert [g.key for g in summary.groups] == ["anthropic", "openai"]
    assert summary.groups[0].cost_usd == pytest.approx(0.35)
    assert summary.groups[0].runs == 2
    prompts = {g.key: (g.cost_usd, g.runs) for g in summary.by_prompt}
    assert prompts["prioritize"] == (pytest.approx(0.35), 2)
    assert prompts["report"] == (pytest.approx(0.10), 1)


def test_null_cost_counts_as_run_at_zero_dollars(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    _add_run(audit_engine, aid, cost=0.20, status="success")
    _add_run(audit_engine, aid, cost=None, status="failed")

    summary = summarize_cost(audit_engine)

    assert summary.runs == 2  # the failed run still counts as a run
    assert summary.total_usd == pytest.approx(0.20)


def test_since_window_excludes_earlier_runs(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    _add_run(audit_engine, aid, cost=1.00, created_at="2026-05-31 23:59:59")
    _add_run(audit_engine, aid, cost=2.00, created_at="2026-06-01 00:00:00")

    summary = summarize_cost(audit_engine, start=date(2026, 6, 1))

    assert summary.runs == 1
    assert summary.total_usd == pytest.approx(2.00)


def test_month_window_is_half_open(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    _add_run(audit_engine, aid, cost=1.00, created_at="2026-05-31 12:00:00")  # before
    _add_run(audit_engine, aid, cost=2.00, created_at="2026-06-15 12:00:00")  # in
    _add_run(audit_engine, aid, cost=4.00, created_at="2026-07-01 00:00:00")  # after (exclusive)

    summary = summarize_cost(audit_engine, start=date(2026, 6, 1), end=date(2026, 7, 1))

    assert summary.runs == 1
    assert summary.total_usd == pytest.approx(2.00)


def test_cost_cli_reports_totals(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    seed: Callable[..., uuid.UUID],
) -> None:
    monkeypatch.setenv("KVSEO_DATA_DIR", str(tmp_path_factory.mktemp("data")))
    from kvseo.config import paths

    db = paths.db_path()
    migrate(db)
    engine = get_engine(db)
    aid = seed(engine)
    _add_run(engine, aid, provider="anthropic", cost=0.30)
    _add_run(engine, aid, provider="openai", cost=0.10)

    result = runner.invoke(app, ["cost"])

    assert result.exit_code == 0
    assert "$0.40" in result.output
    assert "anthropic" in result.output
    assert "openai" in result.output


def test_cost_cli_empty_db_is_graceful(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KVSEO_DATA_DIR", str(tmp_path_factory.mktemp("empty")))
    result = runner.invoke(app, ["cost"])
    assert result.exit_code == 0
    assert "No advisor runs" in result.output


def test_cost_cli_rejects_bad_by(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KVSEO_DATA_DIR", str(tmp_path_factory.mktemp("bad")))
    result = runner.invoke(app, ["cost", "--by", "nonsense"])
    assert result.exit_code == 2
    assert "unsupported --by" in result.output
