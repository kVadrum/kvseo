"""Scoring algorithm (04-audit-engine.md §5)."""

from __future__ import annotations

from kvseo.core.audit.checks._base import CheckResult
from kvseo.core.audit.scoring import score


def _r(verdict: str, severity: str) -> CheckResult:
    return CheckResult("x", verdict, severity, {}, "")  # type: ignore[arg-type]


def test_all_pass_is_100() -> None:
    assert score([_r("pass", "fail"), _r("pass", "warn"), _r("pass", "info")]) == 100


def test_all_fail_is_zero() -> None:
    assert score([_r("fail", "fail"), _r("fail", "warn")]) == 0


def test_warn_is_half_credit() -> None:
    # one warn-severity check, warn verdict → 0.5 of weight 2 over 2 → 50.
    assert score([_r("warn", "warn")]) == 50


def test_skip_and_error_excluded() -> None:
    # skip/error drop out of both numerator and denominator; only the pass counts.
    assert score([_r("pass", "fail"), _r("skip", "fail"), _r("error", "info")]) == 100


def test_weighting_favors_severity() -> None:
    # pass on a fail-severity (weight 3) + fail on an info-severity (weight 1):
    # earned 3 / possible 4 = 75.
    assert score([_r("pass", "fail"), _r("fail", "info")]) == 75


def test_empty_is_zero() -> None:
    assert score([]) == 0
