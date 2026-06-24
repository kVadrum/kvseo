"""Core Web Vitals checks — read PSI from context (04-audit-engine.md §6).

Prefer field data (CrUX), fall back to lab data where it exists. INP has no lab
fallback (Lighthouse reports TBT, not INP), so it skips without field data. All
cwv.* checks skip when PSI is unavailable; the score is computed over the rest.
"""

from __future__ import annotations

from kvseo.core.audit.checks._base import AuditContext, CheckFn, CheckResult, Verdict
from kvseo.core.audit.document import ParsedDocument

# Google's "good" CWV bands are inclusive of the boundary (LCP ≤ 2500ms,
# INP ≤ 200ms, CLS ≤ 0.1), so the verdicts below compare with <=, not <.
_LCP_THRESHOLD_MS = 2500
_INP_THRESHOLD_MS = 200
_CLS_THRESHOLD = 0.1


def _skip(check_id: str) -> CheckResult:
    return CheckResult(check_id, "skip", "fail", {"reason": "psi_unavailable"}, "CWV skipped — PSI unavailable")


def cwv_lcp(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    psi = ctx.psi_result
    if psi is None:
        return _skip("cwv.lcp")
    if psi.field_lcp_ms is not None:
        value, source = psi.field_lcp_ms, "field"
    else:
        value, source = psi.lab_lcp_ms, "lab"
    data = {
        "lcp_ms": value,
        "source": source,
        "threshold": _LCP_THRESHOLD_MS,
        "origin_fallback": psi.field_origin_fallback,
    }
    verdict: Verdict = "pass" if value <= _LCP_THRESHOLD_MS else "fail"
    return CheckResult("cwv.lcp", verdict, "fail", data, f"LCP {value}ms ({source}) vs {_LCP_THRESHOLD_MS}ms")


def cwv_inp(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    psi = ctx.psi_result
    if psi is None:
        return _skip("cwv.inp")
    if psi.field_inp_ms is None:
        return CheckResult("cwv.inp", "skip", "fail", {"reason": "no_inp_data"}, "INP unavailable (no field data)")
    data = {"inp_ms": psi.field_inp_ms, "source": "field", "threshold": _INP_THRESHOLD_MS}
    verdict: Verdict = "pass" if psi.field_inp_ms <= _INP_THRESHOLD_MS else "fail"
    return CheckResult("cwv.inp", verdict, "fail", data, f"INP {psi.field_inp_ms}ms (field) vs {_INP_THRESHOLD_MS}ms")


def cwv_cls(doc: ParsedDocument, ctx: AuditContext) -> CheckResult:
    psi = ctx.psi_result
    if psi is None:
        return _skip("cwv.cls")
    if psi.field_cls is not None:
        value, source = psi.field_cls, "field"
    else:
        value, source = psi.lab_cls, "lab"
    data = {"cls": value, "source": source, "threshold": _CLS_THRESHOLD, "origin_fallback": psi.field_origin_fallback}
    verdict: Verdict = "pass" if value <= _CLS_THRESHOLD else "fail"
    return CheckResult("cwv.cls", verdict, "fail", data, f"CLS {value} ({source}) vs {_CLS_THRESHOLD}")


CHECKS: list[CheckFn] = [cwv_lcp, cwv_inp, cwv_cls]
