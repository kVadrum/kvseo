"""Audit check registry (04-audit-engine.md §2).

Explicit registry, not import-magic: a check runs iff it's listed here. Adding
a check is appending to its category module's ``CHECKS`` (or a new module wired
in below). v0.1 ships the on-page surface; deferred (registry-ready): the
info-level extras (headings.keyword, images.dimensions, links.external_rel),
``schema.valid`` (schema.org validation), and ``internal_links.broken`` (network
HEAD checks, default-off per §9).
"""

from __future__ import annotations

from kvseo.core.audit.checks import content, cwv, headings, meta, title
from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult

REGISTRY: list[CheckFn] = [
    *title.CHECKS,
    *meta.CHECKS,
    *headings.CHECKS,
    *content.CHECKS,
    *cwv.CHECKS,
]

__all__ = ["REGISTRY", "AuditContext", "CheckFn", "CheckResult"]
