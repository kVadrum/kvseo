"""Page-level content + technical checks (04-audit-engine.md §2)."""

from __future__ import annotations

from urllib.parse import urlparse

from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult
from kvseo.core.audit.document import ParsedDocument

_MIN_INTERNAL_LINKS = 3


def https(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    is_https = urlparse(ctx.fetched_url).scheme == "https"
    # Mixed content = insecure sub-resources on a secure page. Images are the
    # sub-resources the parser shim exposes today.
    mixed = [img.src for img in doc.images() if img.src.startswith("http://")] if is_https else []
    data = {"https": is_https, "mixed_content": mixed}
    if is_https and not mixed:
        return CheckResult("https", "pass", "fail", data, "Served over HTTPS, no mixed content")
    if not is_https:
        return CheckResult("https", "fail", "fail", data, "Not served over HTTPS")
    return CheckResult("https", "fail", "fail", data, f"{len(mixed)} insecure (http://) resource(s)")


def viewport(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    content = doc.meta_name("viewport")
    if content:
        return CheckResult("viewport", "pass", "warn", {"viewport": content}, "Mobile viewport set")
    return CheckResult("viewport", "warn", "warn", {"viewport": None}, "No mobile viewport meta tag")


def language(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    lang = doc.html_lang()
    if lang:
        return CheckResult("language", "pass", "info", {"lang": lang}, f"<html lang> is '{lang}'")
    return CheckResult("language", "warn", "info", {"lang": None}, "No <html lang> attribute")


def images_alt(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    images = doc.images()
    missing = [img.src for img in images if not (img.alt or "").strip()]
    data = {"total": len(images), "missing_alt": missing}
    if not missing:
        return CheckResult("images.alt", "pass", "warn", data, "All images have alt text")
    return CheckResult("images.alt", "warn", "warn", data, f"{len(missing)} image(s) missing alt text")


def internal_links_count(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    host = urlparse(ctx.fetched_url).netloc
    internal = [link for link in doc.links() if urlparse(link.href).netloc == host]
    count = len(internal)
    data = {"count": count, "min": _MIN_INTERNAL_LINKS}
    if count >= _MIN_INTERNAL_LINKS:
        return CheckResult("internal_links.count", "pass", "warn", data, f"{count} internal links")
    return CheckResult("internal_links.count", "warn", "warn", data, f"Only {count} internal link(s)")


def schema_presence(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    blocks = doc.schema_blocks()
    types = sorted({t for block in blocks for t in block.types})
    data = {"blocks": len(blocks), "types": types}
    if blocks:
        return CheckResult("schema.presence", "pass", "info", data, f"{len(blocks)} JSON-LD block(s)")
    return CheckResult("schema.presence", "warn", "info", data, "No structured-data (JSON-LD) blocks")


CHECKS: list[CheckFn] = [
    https,
    viewport,
    language,
    images_alt,
    internal_links_count,
    schema_presence,
]
