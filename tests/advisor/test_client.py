"""Advisor client: validation, single-retry, error persistence, key gating.

No network and no real LLM — a fake completion callable returns canned content
(or raises), exercising the parse / retry / persist machinery deterministically.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from kvseo.config.settings import Settings
from kvseo.core.advisor.client import latest_run, prioritize
from kvseo.core.advisor.context import AdvisorError
from kvseo.storage.models import AdvisorOutput as AdvisorOutputORM

_GOOD_ACTION = {
    "rank": 1,
    "title": "Rewrite the title tag around 'ops consulting'",
    "description": "Lead with the keyword; drop to 55 chars.",
    "rationale": "412 impressions, 1.9% CTR at position 14.",
    "expected_impact": "high",
    "effort": "low",
    "evidence": ["title.length", "gsc.queries[0]"],
    "category": "on_page",
}
_GOOD_JSON = json.dumps(
    {
        "summary": "Two leveraged moves.",
        "actions": [_GOOD_ACTION],
        "things_going_well": ["Brand query at position 1.2."],
        "cautions": [],
    }
)


# --- fake LiteLLM response + completion -----------------------------------


class _Msg:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Msg(content)


class _Usage:
    def __init__(self, p: int, c: int) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c


class _Resp:
    def __init__(self, content: str | None, cost: float | None = 0.0012) -> None:
        self.choices = [_Choice(content)]
        self.usage = _Usage(120, 60)
        self._hidden_params = {"response_cost": cost} if cost is not None else {}


def _completion(*scripted: Any) -> Callable[..., Any]:
    """Return an async completion that yields each scripted item in turn. An item
    that is an Exception is raised; otherwise it's wrapped in a fake response."""
    state = {"i": 0}

    async def complete(**_kwargs: Any) -> Any:
        item = scripted[min(state["i"], len(scripted) - 1)]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    return complete


def _settings() -> Settings:
    return Settings()  # anthropic / claude-haiku-4-5 defaults


def _one_row(engine: Engine) -> AdvisorOutputORM:
    with Session(engine) as s:
        return s.scalars(select(AdvisorOutputORM)).one()


async def test_success_persists_validated_output(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    run = await prioritize(aid, engine=audit_engine, settings=_settings(), completion=_completion(_GOOD_JSON))

    assert run.status == "success"
    assert run.output is not None
    assert run.output.actions[0].title.startswith("Rewrite")
    assert run.prompt_tokens == 120
    assert run.estimated_cost_usd == pytest.approx(0.0012)
    row = _one_row(audit_engine)
    assert row.status == "success"
    assert row.audit_run_id == aid
    assert row.output["actions"][0]["category"] == "on_page"


async def test_retry_recovers_from_first_bad_response(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    run = await prioritize(
        aid, engine=audit_engine, settings=_settings(),
        completion=_completion("not json at all", _GOOD_JSON),
    )
    assert run.status == "success"
    assert run.output is not None
    # Exactly one row persisted (the successful run), not one per attempt.
    with Session(audit_engine) as s:
        assert s.scalar(select(func.count()).select_from(AdvisorOutputORM)) == 1


async def test_two_bad_responses_record_invalid_output(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    run = await prioritize(
        aid, engine=audit_engine, settings=_settings(),
        completion=_completion("nope", "still not json"),
    )
    assert run.status == "invalid_output"
    assert run.output is None
    row = _one_row(audit_engine)
    assert row.status == "invalid_output"
    assert row.raw_response == "still not json"  # the last attempt's raw body
    assert row.error


async def test_schema_violation_is_invalid_output(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    # Valid JSON, but an action with no evidence violates R1 → invalid_output.
    bad = json.dumps({"summary": "x", "actions": [{**_GOOD_ACTION, "evidence": []}]})
    aid = seed(audit_engine)
    run = await prioritize(
        aid, engine=audit_engine, settings=_settings(), completion=_completion(bad, bad)
    )
    assert run.status == "invalid_output"


async def test_provider_exception_records_failed(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    aid = seed(audit_engine)
    run = await prioritize(
        aid, engine=audit_engine, settings=_settings(),
        completion=_completion(RuntimeError("503 upstream")),
    )
    assert run.status == "failed"
    assert run.error is not None and "503 upstream" in run.error
    row = _one_row(audit_engine)
    assert row.status == "failed"


def test_latest_run_breaks_same_second_tie_by_insertion_order(
    audit_engine: Engine, seed: Callable[..., uuid.UUID]
) -> None:
    # Two runs sharing a created_at second (a failed run, then an immediate
    # successful rerun): latest_run must return the later-inserted success row,
    # not let the tie resolve arbitrarily to the stale failure.
    aid = seed(audit_engine)
    same_second = "2026-06-24 12:00:00"
    good = json.loads(_GOOD_JSON)
    with Session(audit_engine) as s:
        s.add(
            AdvisorOutputORM(
                audit_run_id=aid, prompt_id="prioritize", provider="anthropic",
                model="m", status="invalid_output", raw_response="garbage",
                created_at=same_second,
            )
        )
        s.flush()  # force the failed row to take the lower rowid
        s.add(
            AdvisorOutputORM(
                audit_run_id=aid, prompt_id="prioritize", provider="anthropic",
                model="m", status="success", output=good, created_at=same_second,
            )
        )
        s.commit()

    run = latest_run(aid, audit_engine)
    assert run is not None
    assert run.status == "success"
    assert run.output is not None


async def test_missing_key_raises_before_any_call(
    audit_engine: Engine, seed: Callable[..., uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Real path (completion=None) with no provider key must reject pre-flight,
    # writing no row, rather than attempting a doomed network call.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("kvseo.core.advisor.client.get_secret", lambda _key: None)
    aid = seed(audit_engine)
    with pytest.raises(AdvisorError, match="no API key"):
        await prioritize(aid, engine=audit_engine, settings=_settings())
    with Session(audit_engine) as s:
        assert s.scalar(select(func.count()).select_from(AdvisorOutputORM)) == 0
