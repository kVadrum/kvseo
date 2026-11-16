"""Advisor output schemas (pydantic). Canonical shape: 05-advisor-prompts.md §4.

These are the contract the model is held to: a response that doesn't validate
triggers one retry, then a logged ``invalid_output`` row (§6.3). Two disciplines
are baked in:

* **R1 — every claim cites evidence that exists.** A :class:`PriorityAction`
  with no evidence refs is rejected by a validator, not merely discouraged in
  the prompt. Validated with an :class:`EvidenceIndex` (pydantic validation
  context), each ref must also resolve to something the model was shown: refs
  that don't are dropped, and an action left with none is rejected — a citation
  to a check that never ran is how a hallucinated claim looks authoritative.
* **R8 — schema evolution is additive.** New fields ship with defaults and are
  never required; field removal is a major bump; every persisted row carries
  ``schema_version`` so old rows stay readable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field, ValidationInfo, model_validator

ADVISOR_OUTPUT_SCHEMA_VERSION = 1

Impact = Literal["high", "medium", "low"]
Effort = Literal["low", "medium", "high"]
Category = Literal["on_page", "content", "technical", "performance"]

# The prompt teaches `gsc.queries[3]`; the context JSON names the list `gsc_queries`.
_GSC_REF = re.compile(r"gsc(?:\.|_)queries\[(\d+)\]")


@dataclass(frozen=True)
class EvidenceIndex:
    """What an evidence ref may point at: the context the model was shown."""

    check_ids: frozenset[str]
    gsc_rows: int
    has_psi: bool

    def resolves(self, ref: str) -> bool:
        ref = ref.strip()
        if ref in self.check_ids:
            return True
        if match := _GSC_REF.fullmatch(ref):
            return int(match.group(1)) < self.gsc_rows
        return self.has_psi and (ref == "psi" or ref.startswith("psi."))


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
    def _require_evidence(self, info: ValidationInfo) -> PriorityAction:
        # R1: an action with nothing to cite is not actionable — it's an opinion.
        refs = [ref for ref in self.evidence if ref.strip()]
        if not refs:
            raise ValueError(f"action '{self.title}' cites no evidence (R1)")
        # Only a fresh model reply is validated with an index; a stored output
        # re-read for a report was resolved when it was written.
        index = (info.context or {}).get("evidence")
        if isinstance(index, EvidenceIndex):
            resolved = [ref for ref in refs if index.resolves(ref)]
            if not resolved:
                raise ValueError(
                    f"action '{self.title}' cites evidence absent from the context: {refs} (R1)"
                )
            self.evidence = resolved
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
