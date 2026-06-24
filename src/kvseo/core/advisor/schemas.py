"""Advisor output schemas (pydantic). Canonical shape: 05-advisor-prompts.md §4.

These are the contract the model is held to: a response that doesn't validate
triggers one retry, then a logged ``invalid_output`` row (§6.3). Two disciplines
are baked in:

* **R1 — every claim cites evidence.** A :class:`PriorityAction` with no
  evidence refs is rejected by a validator, not merely discouraged in the
  prompt. The advisor exists to be auditable; an unsourced action defeats it.
* **R8 — schema evolution is additive.** New fields ship with defaults and are
  never required; field removal is a major bump; every persisted row carries
  ``schema_version`` so old rows stay readable.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ADVISOR_OUTPUT_SCHEMA_VERSION = 1

Impact = Literal["high", "medium", "low"]
Effort = Literal["low", "medium", "high"]
Category = Literal["on_page", "content", "technical", "performance"]


class PriorityAction(BaseModel):
    """One ranked recommendation. Ranked by expected impact over effort; every
    action points back at the context rows that justify it (R1)."""

    rank: int
    title: str  # short imperative — "Tighten the title tag"
    description: str  # 1-3 sentences: what to change
    rationale: str  # why it matters, citing the evidence
    expected_impact: Impact
    effort: Effort
    evidence: list[str] = Field(
        default_factory=list,
        description="check IDs (e.g. 'title.length') and/or connector refs (e.g. 'gsc.queries[0]')",
    )
    category: Category

    @model_validator(mode="after")
    def _require_evidence(self) -> PriorityAction:
        # R1: an action with nothing to cite is not actionable — it's an opinion.
        if not [ref for ref in self.evidence if ref.strip()]:
            raise ValueError(f"action '{self.title}' cites no evidence (R1)")
        return self


class PrioritizationOutput(BaseModel):
    """The advisor's ranked action list for one audit (05 §4.1)."""

    schema_version: int = ADVISOR_OUTPUT_SCHEMA_VERSION
    summary: str = ""
    actions: list[PriorityAction] = Field(default_factory=list)
    things_going_well: list[str] = Field(default_factory=list)
    cautions: list[str] = Field(default_factory=list)


class ReportNarrative(BaseModel):
    """Client-facing narrative copy for a monthly report (05 §4.2).

    The prompt ships in v0.1; the *valuable* trend narrative needs ~4 weeks of
    audit history before it has anything to compare against (05 §1)."""

    schema_version: int = ADVISOR_OUTPUT_SCHEMA_VERSION
    executive_summary: str = ""
    trend_observations: list[str] = Field(default_factory=list)
    wins: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_month_focus: str = ""
