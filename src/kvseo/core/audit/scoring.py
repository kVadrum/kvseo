"""Audit scoring (04-audit-engine.md §5).

A deliberately crude 0-100: "did this get better than last month?", not a
calibrated grade. pass = full credit, warn = half, fail = none; skip/error are
excluded from numerator and denominator alike, so the score reflects only what
was actually measured.
"""

from __future__ import annotations

from kvseo.core.audit.checks import CheckResult

_SEVERITY_WEIGHT = {"info": 1, "warn": 2, "fail": 3}


def score(results: list[CheckResult]) -> int:
    earned = 0.0
    possible = 0.0
    for result in results:
        if result.verdict in ("skip", "error"):
            continue
        weight = _SEVERITY_WEIGHT[result.severity]
        possible += weight
        if result.verdict == "pass":
            earned += weight
        elif result.verdict == "warn":
            earned += weight * 0.5
        # fail contributes 0
    if possible == 0:
        return 0
    return round(100 * earned / possible)
