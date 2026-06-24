"""Meta-tag checks (04-audit-engine.md §2)."""

from __future__ import annotations

from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult
from kvseo.core.audit.document import ParsedDocument

_DESC_MIN, _DESC_MAX = 50, 160
_OG_REQUIRED = ("og:title", "og:description", "og:image")


def description_presence(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    desc = doc.meta_name("description")
    if desc:
        return CheckResult(
            "meta.description.presence", "pass", "fail", {"length": len(desc)}, "Meta description present"
        )
    return CheckResult("meta.description.presence", "fail", "fail", {}, "No meta description")


def description_length(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    desc = doc.meta_name("description")
    if not desc:
        return CheckResult(
            "meta.description.length", "skip", "warn", {"reason": "no_description"}, "No description to measure"
        )
    length = len(desc)
    data = {"length": length, "min": _DESC_MIN, "max": _DESC_MAX}
    if _DESC_MIN <= length <= _DESC_MAX:
        return CheckResult("meta.description.length", "pass", "warn", data, f"Description {length} chars")
    over = "exceeds" if length > _DESC_MAX else "under"
    return CheckResult(
        "meta.description.length", "warn", "warn", data,
        f"Description {length} chars {over} the {_DESC_MIN}-{_DESC_MAX} range",
    )


def robots(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    directive = (doc.meta_name("robots") or "").strip()
    blocked = "noindex" in directive.lower()
    data = {"robots": directive or None}
    if blocked:
        return CheckResult("meta.robots", "fail", "fail", data, f"Page is non-indexable (robots: {directive})")
    return CheckResult("meta.robots", "pass", "fail", data, "Indexing not blocked by meta robots")


def canonical(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    href = doc.link_rel("canonical")
    if href:
        return CheckResult("meta.canonical", "pass", "warn", {"canonical": href}, f"Canonical set to {href}")
    return CheckResult("meta.canonical", "warn", "warn", {"canonical": None}, "No canonical link")


def open_graph(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    present = {tag: doc.meta_property(tag) is not None for tag in _OG_REQUIRED}
    missing = [tag for tag, ok in present.items() if not ok]
    if not missing:
        return CheckResult("meta.og", "pass", "warn", {"present": list(_OG_REQUIRED)}, "Open Graph tags present")
    return CheckResult(
        "meta.og", "warn", "warn", {"missing": missing}, f"Missing Open Graph tags: {', '.join(missing)}"
    )


CHECKS: list[CheckFn] = [
    description_presence,
    description_length,
    robots,
    canonical,
    open_graph,
]
