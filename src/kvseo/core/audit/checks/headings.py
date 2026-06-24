"""Heading-structure checks (04-audit-engine.md §2)."""

from __future__ import annotations

from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult
from kvseo.core.audit.document import ParsedDocument


def h1_presence(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    h1s = [h.text for h in doc.headings() if h.level == 1]
    data = {"count": len(h1s), "h1s": h1s}
    if len(h1s) == 1:
        return CheckResult("headings.h1.presence", "pass", "warn", data, "Exactly one <h1>")
    if not h1s:
        return CheckResult("headings.h1.presence", "warn", "warn", data, "No <h1> on the page")
    return CheckResult("headings.h1.presence", "warn", "warn", data, f"{len(h1s)} <h1> tags found")


def hierarchy(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    levels = [h.level for h in doc.headings()]
    skips = []
    previous = 0
    for level in levels:
        # A jump deeper than one level (e.g. h1 -> h3) skips a level.
        if previous and level > previous + 1:
            skips.append({"from": previous, "to": level})
        previous = level
    data = {"skips": skips}
    if not skips:
        return CheckResult("headings.hierarchy", "pass", "warn", data, "Heading hierarchy is well-formed")
    return CheckResult("headings.hierarchy", "warn", "warn", data, f"{len(skips)} skipped heading level(s)")


CHECKS: list[CheckFn] = [h1_presence, hierarchy]
