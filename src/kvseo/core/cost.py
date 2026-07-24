"""Cost accounting over stored advisor runs (06 §4.9).

Every advisor call persists its token usage and estimated cost to
``advisor_outputs`` when it runs; this module rolls those rows back up. It is a
pure read — it opens a session, aggregates in Python (advisor runs are few), and
returns a dataclass. No writes, no model calls, no token spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.storage.models import AdvisorOutput

GroupBy = Literal["provider", "model", "day"]


@dataclass(frozen=True)
class CostGroup:
    """One breakdown row: a group key, its summed spend, and its run count."""

    key: str
    cost_usd: float
    runs: int


@dataclass(frozen=True)
class CostSummary:
    """A rolled-up view of advisor spend over a window (06 §4.9)."""

    total_usd: float
    runs: int
    by: GroupBy
    groups: list[CostGroup]  # broken down by `by`
    by_prompt: list[CostGroup]  # always also broken down by prompt_id
    start: date | None  # inclusive
    end: date | None  # exclusive


def summarize_cost(
    engine: Engine,
    *,
    start: date | None = None,
    end: date | None = None,
    by: GroupBy = "provider",
) -> CostSummary:
    """Aggregate ``advisor_outputs`` cost over ``[start, end)`` (both optional).

    ``created_at`` is stored as an ISO ``YYYY-MM-DD HH:MM:SS`` string, so a
    lexical ``>=`` / ``<`` against a date's ISO form is a correct calendar bound.
    A NULL ``estimated_cost_usd`` (a failed/invalid run recorded no cost) counts
    as \\$0 but still counts as a run.
    """
    with Session(engine) as session:
        stmt = select(
            AdvisorOutput.provider,
            AdvisorOutput.model,
            AdvisorOutput.prompt_id,
            AdvisorOutput.created_at,
            AdvisorOutput.estimated_cost_usd,
        )
        if start is not None:
            stmt = stmt.where(AdvisorOutput.created_at >= start.isoformat())
        if end is not None:
            stmt = stmt.where(AdvisorOutput.created_at < end.isoformat())
        rows = session.execute(stmt).all()

    dim_cost: dict[str, float] = {}
    dim_runs: dict[str, int] = {}
    prompt_cost: dict[str, float] = {}
    prompt_runs: dict[str, int] = {}
    total = 0.0

    for provider, model, prompt_id, created_at, cost_usd in rows:
        cost = cost_usd or 0.0
        total += cost
        key = {"provider": provider, "model": model}.get(by, created_at[:10])
        dim_cost[key] = dim_cost.get(key, 0.0) + cost
        dim_runs[key] = dim_runs.get(key, 0) + 1
        prompt_cost[prompt_id] = prompt_cost.get(prompt_id, 0.0) + cost
        prompt_runs[prompt_id] = prompt_runs.get(prompt_id, 0) + 1

    return CostSummary(
        total_usd=round(total, 6),
        runs=len(rows),
        by=by,
        groups=_rank(dim_cost, dim_runs),
        by_prompt=_rank(prompt_cost, prompt_runs),
        start=start,
        end=end,
    )


def _rank(cost: dict[str, float], runs: dict[str, int]) -> list[CostGroup]:
    """Group rows into cost-descending order (ties broken by key for stability)."""
    return [
        CostGroup(key=key, cost_usd=round(cost[key], 6), runs=runs[key])
        for key in sorted(cost, key=lambda k: (-cost[k], k))
    ]
