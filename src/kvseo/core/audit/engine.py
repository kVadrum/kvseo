"""Audit engine entry point (built against 04-audit-engine.md).

Scaffold stub: fixes the call shape the CLI and advisor depend on. The real
engine fetches the URL, runs the on-page checks, pulls CWV via PSI, scores, and
persists an ``audit_runs`` row.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuditResult:
    """Placeholder result. Real shape is defined in the audit-engine build."""

    url: str


def run(url: str) -> AuditResult:
    """Run the on-page audit against a single URL."""
    raise NotImplementedError("audit engine lands in the v0.1 build")
