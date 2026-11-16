"""Advisor output schemas: the R1 evidence-citation rule and JSON round-trip."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from kvseo.core.advisor.schemas import (
    ADVISOR_OUTPUT_SCHEMA_VERSION,
    EvidenceIndex,
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


_INDEX = EvidenceIndex(check_ids=frozenset({"title.length", "cwv.lcp"}), gsc_rows=2, has_psi=False)


@pytest.mark.parametrize(
    ("ref", "resolves"),
    [
        ("title.length", True),
        (" title.length ", True),
        ("title.lenght", False),  # a near-miss is not the check
        ("gsc.queries[1]", True),
        ("gsc_queries[0]", True),  # the context JSON's own spelling
        ("gsc.queries[2]", False),  # only two rows were shown
        ("gsc.queries[-1]", False),
        ("psi.field_lcp_ms", False),  # no PSI in this context
    ],
)
def test_evidence_index_resolves_only_what_the_model_was_shown(ref: str, resolves: bool) -> None:
    assert _INDEX.resolves(ref) is resolves


def test_unresolvable_refs_are_dropped_from_a_cited_action() -> None:
    action = PriorityAction.model_validate(
        {**_VALID, "evidence": ["title.length", "meta.keywords", "gsc.queries[9]"]},
        context={"evidence": _INDEX},
    )
    assert action.evidence == ["title.length"]


def test_an_action_citing_only_absent_evidence_is_rejected() -> None:
    with pytest.raises(ValidationError, match="absent from the context"):
        PriorityAction.model_validate(
            {**_VALID, "evidence": ["meta.keywords", "gsc.queries[9]"]}, context={"evidence": _INDEX}
        )


def test_a_stored_output_revalidates_without_an_index() -> None:
    # Reports re-read stored outputs with no context; resolution already happened.
    action = PriorityAction.model_validate({**_VALID, "evidence": ["meta.keywords"]})
    assert action.evidence == ["meta.keywords"]
