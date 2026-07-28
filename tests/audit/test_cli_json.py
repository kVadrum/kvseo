"""``kvseo audit --json`` must represent advisor failures, not swallow them.

The advisor is a soft stage: a missing key or an oversized context leaves the
deterministic audit standing. But under ``--json`` the human-facing stderr line
used to be suppressed *and* the document carried no advisor field, so a caller
piping the output had no way to tell "the advisor was never attempted" from
"the advisor failed".
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.engine import Engine

from kvseo.cli import audit as audit_cli
from kvseo.core.advisor.context import AdvisorError
from kvseo.core.audit.engine import AuditResult


def _result() -> AuditResult:
    return AuditResult(
        id=uuid.uuid4(),
        url="https://kemek.net/services",
        fetched_url="https://kemek.net/services",
        status="complete",
        failure_reason=None,
        keyword="ops consulting",
        score=72,
        page_title="Operations Consulting — KeMeK",
        page_status_code=200,
        fetch_duration_ms=180,
    )


def test_json_carries_advisor_error() -> None:
    payload = json.loads(audit_cli._audit_json(_result(), "no provider key configured"))
    assert payload["advisor_error"] == "no provider key configured"
    assert payload["score"] == 72  # the audit itself is unaffected


def test_json_key_present_and_null_on_success() -> None:
    """Always-present key: a consumer can read it without an existence check."""
    payload = json.loads(audit_cli._audit_json(_result(), None))
    assert "advisor_error" in payload
    assert payload["advisor_error"] is None


def test_run_advisor_returns_message_and_warns_on_stderr(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _boom(*_args: object, **_kwargs: object) -> object:
        raise AdvisorError("no provider key configured")

    monkeypatch.setattr(audit_cli, "prioritize", _boom)
    run, error = audit_cli._run_advisor(uuid.uuid4(), audit_engine)

    assert run is None
    assert error == "no provider key configured"
    # stderr stays populated even for JSON callers — the document goes to stdout,
    # so the warning cannot corrupt it.
    assert "advisor skipped — no provider key configured" in capsys.readouterr().err


def test_run_advisor_reports_no_error_on_success(
    audit_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(*_args: object, **_kwargs: object) -> str:
        return "advisor-run"

    monkeypatch.setattr(audit_cli, "prioritize", _ok)
    run, error = audit_cli._run_advisor(uuid.uuid4(), audit_engine)

    assert run == "advisor-run"
    assert error is None
