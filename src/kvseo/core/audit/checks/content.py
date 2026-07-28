"""Page-level content + technical checks (04-audit-engine.md §2)."""

from __future__ import annotations

from urllib.parse import urlparse

from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult
from kvseo.core.audit.document import ParsedDocument, is_external, is_internal

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
    internal = [link for link in doc.links() if is_internal(link.href, host)]
    count = len(internal)
    data = {"count": count, "min": _MIN_INTERNAL_LINKS}
    if count >= _MIN_INTERNAL_LINKS:
        return CheckResult("internal_links.count", "pass", "warn", data, f"{count} internal links")
    return CheckResult("internal_links.count", "warn", "warn", data, f"Only {count} internal link(s)")


def internal_links_broken(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    """Internal links resolve to 2xx (04 §2 / §9, warn).

    Pure like every other check: the engine does the probing (only under
    ``--check-internal-links``) and passes the results in. A None context
    field means the probe was never requested, which is a skip — reporting
    "no broken links" for links nobody checked would be a false all-clear.
    """
    statuses = ctx.internal_link_status
    if statuses is None:
        return CheckResult(
            "internal_links.broken", "skip", "warn", {"reason": "not_requested"},
            "Internal-link check not run (pass --check-internal-links)",
        )
    if not statuses:
        return CheckResult("internal_links.broken", "skip", "warn", {"checked": 0}, "No internal links to check")

    broken = {url: code for url, code in statuses.items() if code is None or code >= 400}
    data = {
        "checked": len(statuses),
        # JSON columns need string values here; None becomes "unreachable".
        "broken": {url: (str(code) if code is not None else "unreachable") for url, code in broken.items()},
    }
    if not broken:
        return CheckResult(
            "internal_links.broken", "pass", "warn", data, f"All {len(statuses)} internal links resolve"
        )
    return CheckResult(
        "internal_links.broken", "warn", "warn", data,
        f"{len(broken)} of {len(statuses)} internal link(s) broken",
    )


def schema_presence(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    blocks = doc.schema_blocks()
    types = sorted({t for block in blocks for t in block.types})
    data = {"blocks": len(blocks), "types": types}
    if blocks:
        return CheckResult("schema.presence", "pass", "info", data, f"{len(blocks)} JSON-LD block(s)")
    return CheckResult("schema.presence", "warn", "info", data, "No structured-data (JSON-LD) blocks")


def images_dimensions(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    """All <img> carry width/height (04 §2, info).

    Intrinsic dimensions let the browser reserve space before the image loads,
    which is the cheapest fix for layout shift — so this is the on-page
    counterpart to a poor ``cwv.cls``.
    """
    images = doc.images()
    if not images:
        return CheckResult("images.dimensions", "skip", "info", {"total": 0}, "No images on the page")
    missing = [img.src for img in images if not img.width or not img.height]
    data = {"total": len(images), "missing_dimensions": missing}
    if not missing:
        return CheckResult("images.dimensions", "pass", "info", data, "All images declare width and height")
    return CheckResult(
        "images.dimensions", "warn", "info", data, f"{len(missing)} image(s) missing width/height"
    )


def links_external_rel(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    """External target="_blank" links carry rel="noopener" (04 §2, info).

    Without it the opened page gets a ``window.opener`` handle back to this
    one — a tabnabbing vector. Modern browsers imply noopener for _blank, so
    this is defence for older clients and an explicitness signal, hence info.
    Only external links are considered: same-host popups hand a handle to a
    page the author already controls.
    """
    host = urlparse(ctx.fetched_url).netloc
    offenders = [
        link.href
        for link in doc.links()
        if link.target.lower() == "_blank"
        and is_external(link.href, host)
        and "noopener" not in link.rel.lower()
    ]
    data = {"offenders": offenders}
    if not offenders:
        return CheckResult(
            "links.external_rel", "pass", "info", data, "External _blank links use rel=noopener"
        )
    return CheckResult(
        "links.external_rel", "warn", "info", data, f"{len(offenders)} external _blank link(s) without rel=noopener"
    )


def schema_valid(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    """Structured-data blocks parse and declare a schema.org type (04 §2, warn).

    Scope is deliberately structural: JSON parses, ``@type`` is present, and
    ``@context`` points at schema.org. Validating property names against the
    live schema.org vocabulary would mean shipping (and versioning) the
    ontology, which v0.1 does not carry — so a block claiming a real type with
    bogus properties passes here. That limit is worth stating rather than
    implying a depth the check does not have.
    """
    blocks = doc.schema_blocks()
    if not blocks:
        return CheckResult("schema.valid", "skip", "warn", {"blocks": 0}, "No JSON-LD blocks to validate")

    errors: list[dict[str, str]] = []
    for index, block in enumerate(blocks):
        if not block.valid_json:
            errors.append({"block": str(index), "error": "not valid JSON"})
        elif not block.types:
            errors.append({"block": str(index), "error": "no @type"})
        elif not any("schema.org" in c for c in block.contexts):
            errors.append({"block": str(index), "error": "@context is not schema.org"})

    data = {"blocks": len(blocks), "errors": errors}
    if not errors:
        return CheckResult("schema.valid", "pass", "warn", data, f"All {len(blocks)} schema block(s) valid")
    return CheckResult(
        "schema.valid", "warn", "warn", data, f"{len(errors)} of {len(blocks)} schema block(s) invalid"
    )


CHECKS: list[CheckFn] = [
    https,
    viewport,
    language,
    images_alt,
    images_dimensions,
    links_external_rel,
    internal_links_count,
    internal_links_broken,
    schema_presence,
    schema_valid,
]
