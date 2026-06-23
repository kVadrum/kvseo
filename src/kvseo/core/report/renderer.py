"""Report renderer (built against ADR-005 / 05-advisor-prompts.md).

Scaffold stub. v0.1: Markdown (Jinja2 templates) → self-contained HTML with
base64-embedded images and inlined fonts. v0.2 adds PDF (Playwright) and DOCX
(Pandoc) from the same HTML intermediate.
"""

from __future__ import annotations


def render(audit_id: int, fmt: str = "html") -> str:
    """Render a stored audit to a report string in the requested format."""
    raise NotImplementedError("report renderer lands in the v0.1 build")
