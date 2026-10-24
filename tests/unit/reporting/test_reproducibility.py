"""R5 P1–P10 report reproducibility and fill-completeness semantics."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.application.cli import main
from tyrex_pm.execution.evidence import (
    ExecutionRole,
    ManualInterventionRequired,
    SessionOpened,
    SubmissionAttempted,
    SubmissionFailed,
    new_envelope,
)
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.persistence.run_evidence_journal import RunEvidenceRecord
from tyrex_pm.reporting.reproducibility import capture_reproducibility
from tyrex_pm.reporting.run_report import RunReportInput, build_run_report, write_emergency_report
from tyrex_pm.runtime.run_config import load_trading_run_config

NOW = datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc)
CFG = Path("config/runs/z_gap_tiny_live.yaml")


def _repro(**overrides):
    config = load_trading_run_config(CFG)
    payload = capture_reproducibility(
        config=config,
        config_path=CFG,
        cli_flags={"live": True, "out_dir": "var/out"},
        run_instance_id="run-1",
    )
    payload.update(overrides)
    return payload


def _input(**overrides) -> RunReportInput:
    values = dict(
        run_instance_id="run-1",
        configured_run_name="z_gap_tiny_live",
        live_requested=True,
        state=None,
        mutation_attempts=0,
        execution_timeline=(),
        capabilities={},
        market_data=None,
        run_evidence=(),
        runtime_errors=(),
        reproducibility=_repro(),
    )
    values.update(overrides)
    return RunReportInput(**values)


def test_p1_git_missing_is_null(monkeypatch) -> None:
    monkeypatch.setattr(
        "tyrex_pm.reporting.reproducibility.subprocess.run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no git")),
    )
    payload = json.loads(json.dumps(capture_reproducibility(
        config=load_trading_run_config(CFG),
        config_path=CFG,
        cli_flags={"live": False, "out_dir": None},
        run_instance_id="r",
    )))
    assert payload["git_revision"] is None
    assert payload["git_revision"] != "unknown"
    assert payload["git_revision_error"]


def test_p2_source_and_effective_hashes_differ() -> None:
    payload = _repro()
    assert payload["source_config_sha256"]
    assert payload["effective_config_sha256"]
    assert payload["source_config_sha256"] != payload["effective_config_sha256"]


def test_p3_fees_remain_null() -> None:
    report = build_run_report(_input())
    assert report["summary"]["fees"] is None


def test_p4_ambiguous_session_is_incomplete() -> None:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(
        state,
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="m",
            window_id="w",
            token_id="t",
        ),
    )
    from tyrex_pm.execution.evidence import DispatchAuthorized, OrderRequested
    from tyrex_pm.execution.orders import MarketBuyOrderSpec

    state, _ = reduce_execution_event(
        state,
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="req"),
            role=ExecutionRole.ENTRY,
            order=MarketBuyOrderSpec(
                order_id="e1",
                market_id="m",
                instrument_id="t",
                token_id="t",
                spend_amount=Decimal("5"),
                maximum_total_debit=Decimal("5"),
                worst_price=Decimal("0.5"),
            ),
        ),
    )
    state, _ = reduce_execution_event(
        state,
        DispatchAuthorized(
            **new_envelope(session_id="s1", dedupe_key="auth"),
            role=ExecutionRole.ENTRY,
            order_id="e1",
            prepared_order_digest="d",
        ),
    )
    state, _ = reduce_execution_event(
        state,
        SubmissionAttempted(
            **new_envelope(session_id="s1", dedupe_key="att"),
            role=ExecutionRole.ENTRY,
            order_id="e1",
            attempt_id="a1",
        ),
    )
    state, _ = reduce_execution_event(
        state,
        SubmissionFailed(
            **new_envelope(session_id="s1", dedupe_key="fail"),
            role=ExecutionRole.ENTRY,
            order_id="e1",
            attempt_id="a1",
            error_class="timeout",
            message="lost",
            ambiguous=True,
        ),
    )
    report = json.loads(json.dumps(build_run_report(_input(state=state))))
    assert report["summary"]["observed_fills_complete"] is False


def test_p5_no_fill_terminal_is_complete() -> None:
    state = ExecutionSessionState()
    state.phase = ExecutionPhase.COMPLETED_NO_FILL
    state.reconciliation_complete = True
    report = json.loads(json.dumps(build_run_report(_input(state=state, prior_session_risk=False))))
    assert report["summary"]["observed_fills"] == []
    assert report["summary"]["observed_fills_complete"] is True
    assert report["summary"]["reconciliation_complete"] is True
    assert report["summary"]["prior_session_risk"] is False


def test_p6_out_dir_collision_before_create(tmp_path: Path, monkeypatch) -> None:
    created = {"called": False}

    async def boom(**_kwargs):
        created["called"] = True
        raise AssertionError("create must not run")

    monkeypatch.setattr("tyrex_pm.runtime.trading_runtime.run_trading_runtime", boom)
    out = tmp_path / "out"
    out.mkdir()
    (out / "run_summary.json").write_text("{}", encoding="utf-8")
    code = main(
        [
            "run",
            "--config",
            str(CFG),
            "--live",
            "--out-dir",
            str(out),
        ]
    )
    assert code == 2
    assert created["called"] is False


def test_p7_emergency_report_keeps_identity(tmp_path: Path) -> None:
    path = tmp_path / "run_summary.json"
    payload = write_emergency_report(
        path,
        run_instance_id="abc",
        configured_run_name="z_gap_tiny_live",
        live_requested=True,
        mutation_attempts=0,
        fatal_error={"stage": "runtime_creation", "error_type": "RuntimeError", "message": "x"},
        reporting_error=RuntimeError("create failed"),
        reproducibility=_repro(),
    )
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["run_instance_id"] == "abc"
    assert data["reproducibility"]["python_version"]
    assert data["reproducibility"]["source_config_sha256"]
    assert "effective_config" not in data["reproducibility"]
    assert data["run_instance_id"] == "abc"
    assert payload["reproducibility"]["python_version"]


def test_p8_effective_config_has_no_secrets() -> None:
    dumped = json.dumps(_repro())
    assert "PRIVATE_KEY" not in dumped
    assert "POLYMARKET_PRIVATE_KEY" not in dumped


def test_p9_no_session_with_restored_manual_is_prior_risk() -> None:
    report = json.loads(
        json.dumps(build_run_report(_input(state=None, prior_session_risk=True)))
    )
    assert report["summary"]["prior_session_risk"] is True
    assert report["summary"]["observed_fills_complete"] is False
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(
        state,
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="m",
            window_id="w",
            token_id="t",
        ),
    )
    state, _ = reduce_execution_event(
        state,
        ManualInterventionRequired(
            **new_envelope(session_id="s1", dedupe_key="man"),
            reason="leftover",
        ),
    )
    manual = json.loads(json.dumps(build_run_report(_input(state=state))))
    assert manual["summary"]["observed_fills_complete"] is False
    assert manual["summary"]["observed_fills"] == []


def test_p10_no_session_no_prior_risk_is_this_process_complete() -> None:
    records = (
        RunEvidenceRecord(
            sequence=1,
            event_id="e1",
            run_instance_id="run-1",
            event_type="READINESS_CHANGED",
            observed_at=NOW,
            payload={
                "entry_executable": True,
                "execution_infrastructure_ready": True,
                "blockers": [],
            },
        ),
        RunEvidenceRecord(
            sequence=2,
            event_id="e2",
            run_instance_id="run-1",
            event_type="STRATEGY_DECISION",
            observed_at=NOW,
            payload={"strategy_inputs_eligible": True, "reason_code": "INSUFFICIENT_EDGE"},
        ),
    )
    report = json.loads(
        json.dumps(
            build_run_report(
                _input(state=None, prior_session_risk=False, run_evidence=records)
            )
        )
    )
    assert report["summary"]["prior_session_risk"] is False
    assert report["summary"]["observed_fills"] == []
    assert report["summary"]["observed_fills_complete"] is True
    assert report["summary"]["reconciliation_complete"] is None


def test_completed_no_dispatch_is_confirmed_no_mutation() -> None:
    state = ExecutionSessionState()
    state.phase = ExecutionPhase.COMPLETED_NO_DISPATCH
    state.reconciliation_complete = False
    report = json.loads(json.dumps(build_run_report(_input(state=state, prior_session_risk=False))))
    assert report["outcome"] == "COMPLETED_NO_DISPATCH"
    assert report["ok"] is True
    assert report["summary"]["observed_fills_complete"] is True
    assert report["summary"]["reconciliation_complete"] is True


def test_paper_report_keeps_arming_separate_from_real_orders(tmp_path: Path) -> None:
    report = build_run_report(_input(
        live_requested=True, venue="paper", real_orders_authorized=False,
    ))
    assert report["live_requested"] is True
    assert report["venue"] == "paper"
    assert report["real_orders_authorized"] is False
    live = build_run_report(_input(
        live_requested=True, venue="live", real_orders_authorized=True,
    ))
    assert live["real_orders_authorized"] is True
    emergency = write_emergency_report(
        tmp_path / "run_summary.json",
        run_instance_id="r",
        configured_run_name="paper",
        live_requested=True,
        mutation_attempts=0,
        fatal_error={"stage": "probe"},
        reporting_error=RuntimeError("probe"),
        venue="paper",
        real_orders_authorized=False,
    )
    assert emergency["live_requested"] is True
    assert emergency["venue"] == "paper"
    assert emergency["real_orders_authorized"] is False
