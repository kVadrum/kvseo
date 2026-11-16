"""Audit check registry (04-audit-engine.md §2).

Explicit registry, not import-magic: a check runs iff it's listed here. Adding
a check is appending to its category module's ``CHECKS`` (or a new module wired
in below). Every check in the 04 §2 table is registered. Checks never touch the
network: ``internal_links.broken`` reads statuses the engine probed beforehand
(opt-in, §9), and skips when nothing was probed.
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
