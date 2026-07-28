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


def keyword(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    """H1 includes the target keyword (04 §2, info).

    Info rather than warn: a keyword-free H1 is worth knowing about, but the
    H1 is a reader-facing headline and forcing the keyword into it is exactly
    the over-optimization this tool should not push people toward. Every H1 is
    searched, not just the first — a page with two H1s already trips
    ``headings.h1.presence``, and reporting a keyword miss for a heading the
    user did not know existed would be noise on top of noise.
    """
    if not ctx.keyword:
        return CheckResult("headings.keyword", "skip", "info", {"reason": "no_keyword"}, "No target keyword set")
    h1s = [h.text for h in doc.headings() if h.level == 1]
    if not h1s:
        return CheckResult("headings.keyword", "skip", "info", {"reason": "no_h1"}, "No <h1> to check")
    needle = ctx.keyword.lower()
    for text in h1s:
        pos = text.lower().find(needle)
        if pos >= 0:
            return CheckResult(
                "headings.keyword", "pass", "info", {"position": pos}, "H1 includes the target keyword"
            )
    return CheckResult("headings.keyword", "warn", "info", {"position": -1}, "H1 omits the target keyword")


CHECKS: list[CheckFn] = [h1_presence, hierarchy, keyword]
