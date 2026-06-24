"""Shared types for audit checks (kept separate from __init__ to avoid a cycle).

A check is a pure function ``(ParsedDocument, AuditContext) -> CheckResult`` — it
never fetches anything (the engine fetches once and passes the parsed document
in) and never makes recommendations (that's the advisor's job).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from kvseo.connectors.psi import PsiResult
from kvseo.core.audit.document import ParsedDocument

Verdict = Literal["pass", "warn", "fail", "skip", "error"]
Severity = Literal["info", "warn", "fail"]


@dataclass(frozen=True)
class AuditContext:
    fetched_url: str  # post-redirect URL (for https + mixed-content checks)
    keyword: str | None = None  # user-supplied target keyword
    psi_result: PsiResult | None = None  # None → cwv.* checks skip


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    verdict: Verdict
    severity: Severity  # what's at stake if this fails
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""


CheckFn = Callable[[ParsedDocument, AuditContext], CheckResult]
