"""Advisor output schemas (pydantic).

First-draft shape, flagged as open (handoff §3 item 3, risk R8): it will move
under real model output. Discipline from R8 holds — new fields ship with
defaults and are never required; field removal is a major bump; every row
carries ``schema_version``. Canonical schema: 05-advisor-prompts.md §4.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

ADVISOR_OUTPUT_SCHEMA_VERSION = 1


class PriorityItem(BaseModel):
    """One prioritized recommendation; every claim must cite its evidence (R1)."""

    title: str
    rationale: str
    evidence: list[str] = Field(
        default_factory=list,
        description="check IDs / connector rows that substantiate this item",
    )
    priority: int


class PrioritizationOutput(BaseModel):
    """The advisor's top-N action list for one audit."""

    schema_version: int = ADVISOR_OUTPUT_SCHEMA_VERSION
    items: list[PriorityItem] = Field(default_factory=list)
