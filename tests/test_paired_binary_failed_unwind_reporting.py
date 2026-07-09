"""FAILED activation/unwind terminal reporting tests."""

from __future__ import annotations

from decimal import Decimal

from pathlib import Path

from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary.terminal_reporting import (
    activation_unwind_failure_pattern,
    build_terminal_summary_payload,
    finalize_terminal_reporting,
)
from paired_binary_shutdown_helpers import YES, NO, app_cfg, coord_with_books, risk_cfg, strategy_cfg
from tyrex_pm.runtime.config import parse_app_config


def _app_cfg():
    return parse_app_config(
        risk=risk_cfg(),
        strategy=strategy_cfg(
            market_id="btc_5m_20260701_2110",
            condition_id="0xabc",
            event_start_ts=1782939900.0,
            event_end_ts=1782940200.0,
            yes_token_id=YES,
            no_token_id=NO,
            use_fixture_book=False,
        ),
        runtime={
            "execution_mode": "live",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True},
            "execution": {"planner": {"enabled": True}},
            "strategy_lifecycle": {"mode": "market_aware", "max_runtime_s": None},
        },
    )


def _failed_state(**over) -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.FAILED,
        market_id="btc_5m_20260701_2110",
        owner_id="paired_binary",
        yes_token_id=YES,
        no_token_id=NO,
        effective_qty=Decimal("5"),
        yes_entry=Decimal("0.51"),
        no_entry=Decimal("0.47"),
        unwind_block_reason="no_activation_gap_exceeds_loss_budget",
        unwind_attempt_count=2,
        **over,
    )
    state.yes.entry_cash = Decimal("2.55")
    state.yes.entry_qty = Decimal("5")
    state.no.entry_cash = Decimal("2.35")
    state.no.entry_qty = Decimal("5")
    return state


def _activation_fail_rows() -> list[dict]:
    return [
        {
            "fact_type": "paired_binary_market_timing",
            "payload": {
                "market_id": "btc_5m_20260701_2110",
                "event_start_ts": 1.0,
                "event_end_ts": 2.0,
                "phase": "active",
            },
        },
        {"fact_type": "paired_binary_pair_entry_committed", "payload": {}},
        {
            "fact_type": "paired_binary_state_change",
            "payload": {"state": "BOTH_LEGS_FILLED", "to": "BOTH_LEGS_FILLED"},
        },
        {
            "fact_type": "paired_binary_activation_rejected_loss_budget",
            "payload": {"reason": "no_activation_gap_exceeds_loss_budget"},
        },
        {"fact_type": "paired_binary_emergency_unwind_started", "payload": {}},
        {"fact_type": "paired_binary_emergency_unwind_attempt", "payload": {}},
        {
            "fact_type": "paired_binary_emergency_unwind_done",
            "payload": {"attempt_count": 2, "reason": "no_activation_gap_exceeds_loss_budget"},
        },
        {
            "fact_type": "health",
            "payload": {"event": "paired_binary_loop_stopped", "final_state": "FAILED"},
        },
    ]


def test_activation_unwind_failure_pattern_detects_pre_survivor_fail() -> None:
    assert activation_unwind_failure_pattern(_activation_fail_rows()) is True


def test_build_terminal_summary_payload_fields() -> None:
    app = _app_cfg()
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(Path("."))
    state = _failed_state()
    summary = build_terminal_summary_payload(
        run_id=RunId("run-1"),
        state=state,
        cfg=cfg,
        app=app,
        coord=coord,
        fact_rows=_activation_fail_rows(),
    )
    assert summary["final_state"] == "FAILED"
    assert summary["terminal_reason"] == "no_activation_gap_exceeds_loss_budget"
    assert summary["survivor_phase_reached"] is False
    assert summary["emergency_unwind_attempted"] is True
    assert summary["pnl_unavailable_reason"] == "activation_unwind_failed_or_incomplete"


def test_finalize_terminal_reporting_emits_facts(tmp_path) -> None:
    app = _app_cfg()
    cfg = app.paired_binary
    assert cfg is not None
    coord = coord_with_books(tmp_path)
    state = _failed_state()
    sink = JsonlSink(tmp_path / "facts.jsonl")
    rows = _activation_fail_rows()
    with sink:
        finalize_terminal_reporting(
            sink=sink,
            run_id=RunId("run-1"),
            state=state,
            cfg=cfg,
            app=app,
            coord=coord,
            fact_rows=rows,
            yes_book=LegBook(token_id=TokenId(cfg.yes_token_id), bid=None, ask=None, stale=False),
            no_book=LegBook(token_id=TokenId(cfg.no_token_id), bid=None, ask=None, stale=False),
        )
    written = [line for line in (tmp_path / "facts.jsonl").read_text().splitlines() if line.strip()]
    types = {__import__("json").loads(line)["fact_type"] for line in written}
    assert "paired_binary_terminal_summary" in types
    assert "paired_binary_realized_pnl_unavailable" in types
    pnl = next(__import__("json").loads(line) for line in written if "realized_pnl_unavailable" in line)
    assert pnl["payload"]["reason"] == "activation_unwind_failed_or_incomplete"
    assert pnl["payload"]["manual_reconciliation_required"] is True
