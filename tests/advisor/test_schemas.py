"""Advisor output schemas: the R1 evidence-citation rule and JSON round-trip."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from kvseo.core.advisor.schemas import (
    ADVISOR_OUTPUT_SCHEMA_VERSION,
    PrioritizationOutput,
    PriorityAction,
)

_VALID = {
    "rank": 1,
    "title": "Rewrite the title tag",
    "description": "Lead with the target keyword.",
    "rationale": "412 impressions, 1.9% CTR at position 14.",
    "expected_impact": "high",
    "effort": "low",
    "evidence": ["title.length", "gsc.queries[0]"],
    "category": "on_page",
}


def test_action_requires_evidence() -> None:
    bad = {**_VALID, "evidence": []}
    with pytest.raises(ValidationError, match="cites no evidence"):
        PriorityAction.model_validate(bad)


def test_action_rejects_blank_only_evidence() -> None:
    bad = {**_VALID, "evidence": ["  ", ""]}
    with pytest.raises(ValidationError, match="cites no evidence"):
        PriorityAction.model_validate(bad)


def test_action_accepts_cited_evidence() -> None:
    action = PriorityAction.model_validate(_VALID)
    assert action.evidence == ["title.length", "gsc.queries[0]"]


def test_output_round_trip_and_default_version() -> None:
    payload = {
        "summary": "Two leveraged moves.",
        "actions": [_VALID],
        "things_going_well": ["Brand query at position 1.2."],
        "cautions": ["CWV unavailable — could not assess performance."],
    }
    out = PrioritizationOutput.model_validate_json(json.dumps(payload))
    assert out.schema_version == ADVISOR_OUTPUT_SCHEMA_VERSION
    assert len(out.actions) == 1
    assert out.cautions[0].startswith("CWV")


def test_empty_output_is_valid() -> None:
    # An all-defaults output is structurally valid (the model decides content);
    # only per-action evidence is enforced, and there are no actions here.
    out = PrioritizationOutput()
    assert out.actions == []
    assert out.schema_version == ADVISOR_OUTPUT_SCHEMA_VERSION
