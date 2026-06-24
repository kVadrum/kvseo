"""Title checks (04-audit-engine.md §2)."""

from __future__ import annotations

from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult
from kvseo.core.audit.document import ParsedDocument

_MIN, _MAX = 30, 60


def title_presence(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    title = doc.title()
    if title:
        return CheckResult("title.presence", "pass", "fail", {"length": len(title)}, "Title present")
    return CheckResult("title.presence", "fail", "fail", {}, "No non-empty <title> tag")


def title_length(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    title = doc.title()
    if not title:
        return CheckResult("title.length", "skip", "warn", {"reason": "no_title"}, "No title to measure")
    length = len(title)
    data = {"length": length, "min": _MIN, "max": _MAX}
    if _MIN <= length <= _MAX:
        return CheckResult("title.length", "pass", "warn", data, f"Title {length} chars")
    over = "exceeds" if length > _MAX else "under"
    return CheckResult("title.length", "warn", "warn", data, f"Title {length} chars {over} the {_MIN}-{_MAX} range")


def title_keyword(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    if not ctx.keyword:
        return CheckResult("title.keyword", "skip", "warn", {"reason": "no_keyword"}, "No target keyword set")
    title = doc.title() or ""
    pos = title.lower().find(ctx.keyword.lower())
    if pos >= 0:
        return CheckResult("title.keyword", "pass", "warn", {"position": pos}, "Title includes the target keyword")
    return CheckResult("title.keyword", "warn", "warn", {"position": -1}, "Title omits the target keyword")


CHECKS: list[CheckFn] = [title_presence, title_length, title_keyword]
