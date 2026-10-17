"""A failed startup recheck records the issues that block it (once per distinct cause)."""

from __future__ import annotations

import types
from types import SimpleNamespace

from tyrex_pm.execution.startup_reconciliation import IssueSeverity, StartupIssue
from tyrex_pm.runtime.trading_runtime import TradingRuntime


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, kind: str, payload: dict) -> None:
        self.events.append((kind, payload))


def _runtime(active_session: str = "active") -> SimpleNamespace:
    rt = SimpleNamespace(
        lifecycle=SimpleNamespace(session_id=active_session),
        run_recorder=Recorder(),
        _last_recheck_failure_key=None,
    )
    for name in ("_recheck_blocking_issues", "_recheck_keeps_readiness", "_record_recheck_failure"):
        setattr(rt, name, types.MethodType(getattr(TradingRuntime, name), rt))
    return rt


def _issue(
    code: str, session: str, severity: IssueSeverity = IssueSeverity.BLOCKED
) -> StartupIssue:
    return StartupIssue(
        issue_id=f"{code}:{session}",
        severity=severity,
        code=code,
        summary=f"{code} summary",
        session_id=session,
    )


def _assessment(*issues: StartupIssue) -> SimpleNamespace:
    return SimpleNamespace(ready=not issues, issues=issues)


def test_blocking_issues_exclude_auto_and_active_session_exposure() -> None:
    rt = _runtime()
    a = _assessment(
        _issue("EXTERNAL_DISPOSAL", "old", IssueSeverity.AUTO),
        _issue("OWNED_POSITION", "active"),
        _issue("AWAITING_RESOLUTION", "old-session-0cdf5f07"),
    )
    blocking = rt._recheck_blocking_issues(a)
    assert [i.code for i in blocking] == ["AWAITING_RESOLUTION"]
    assert not rt._recheck_keeps_readiness(a)
    assert rt._recheck_keeps_readiness(_assessment(_issue("OWNED_POSITION", "active")))


def test_failure_is_recorded_with_details_once_per_cause() -> None:
    rt = _runtime()
    issues = [_issue("AWAITING_RESOLUTION", "old-session-0cdf5f07")]
    summary = rt._record_recheck_failure(issues)
    rt._record_recheck_failure(issues)  # same cause on the next recheck
    assert "AWAITING_RESOLUTION(0cdf5f07)" in summary
    recorded = [p for k, p in rt.run_recorder.events if k == "STARTUP_RECHECK_FAILED"]
    assert len(recorded) == 1
    assert recorded[0]["issues"][0]["code"] == "AWAITING_RESOLUTION"
    assert recorded[0]["issues"][0]["summary"] == "AWAITING_RESOLUTION summary"
    rt._record_recheck_failure([], error="TimeoutError: rpc")  # a different cause is recorded again
    assert len([k for k, _ in rt.run_recorder.events if k == "STARTUP_RECHECK_FAILED"]) == 2
