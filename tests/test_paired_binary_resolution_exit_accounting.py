"""Resolution-based survivor exit accounting tests."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
from tyrex_pm.strategies.paired_binary import facts as pb_facts
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.resolution_exit import (
    detect_survivor_resolution_exit,
    resolution_accounting_payload,
)
from tyrex_pm.strategies.paired_binary.state import LegRuntime, PairedBinaryPhase, PairedBinaryRuntimeState


def _cfg() -> PairedBinaryStrategyConfig:
    return PairedBinaryStrategyConfig(
        enabled=True,
        owner_id="paired_binary",
        market_id="btc_5m_20260701_2010",
        yes_token_id="1" * 64,
        no_token_id="2" * 64,
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.09"),
        pair_take_profit_pct=Decimal("0.3"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=True,
        max_holding_time_s=300,
        entry_order_style="FAK",
        exit_order_style="FAK",
        entry_fill_timeout_s=60,
        abort_unpaired_entry=True,
        unwind_partial_entry=True,
        min_effective_pair_qty=Decimal("5"),
        run_once=False,
        max_markets=1,
        tick_interval_s=0.2,
        max_book_age_s=5,
        condition_id="0xcond",
        event_start_ts=1_000_000.0,
        event_end_ts=1_000_300.0,
    )


def test_detect_survivor_resolution_only_no_without_oms_exit() -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.ONLY_NO_ACTIVE, effective_qty=Decimal("5"))
    state.yes.exit_cash = Decimal("1.8")
    state.yes.exit_submitted = True
    state.no.exit_cash = None
    state.no.exit_submitted = False
    ctx = detect_survivor_resolution_exit(
        state,
        phase_before=PairedBinaryPhase.ONLY_NO_ACTIVE,
        yes_qty=Decimal("0"),
        no_qty=Decimal("0"),
        venue_yes_qty=Decimal("0"),
        venue_no_qty=Decimal("0"),
    )
    assert ctx is not None
    assert ctx.survivor_leg == "no"
    assert ctx.resolution_detected_reason == "venue_position_zero_without_oms_exit"


def test_emit_resolution_facts_and_pnl_reason(tmp_path) -> None:
    import json

    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE, effective_qty=Decimal("5"))
    state.yes.exit_cash = Decimal("1.8")
    state.yes.exit_submitted = True
    state.no.exit_cash = None
    state.no.exit_submitted = False
    yes = LegBook(bid=Decimal("0.5"), ask=Decimal("0.51"), stale=False, token_id="1" * 64)
    no = LegBook(bid=Decimal("0.6"), ask=Decimal("0.61"), stale=False, token_id="2" * 64)
    cfg = _cfg()
    ctx = detect_survivor_resolution_exit(
        state,
        phase_before=PairedBinaryPhase.ONLY_NO_ACTIVE,
        yes_qty=Decimal("0"),
        no_qty=Decimal("0"),
        venue_yes_qty=Decimal("0"),
        venue_no_qty=Decimal("0"),
    )
    assert ctx is not None
    acct = resolution_accounting_payload(ctx, cfg=cfg, state_after="DONE")
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        pb_facts.emit_resolution_exit_accounting(
            sink, RunId("r1"), state, yes, no, cfg=cfg, accounting_payload=acct
        )
        pb_facts.emit_realized_pnl_unavailable(sink, RunId("r1"), state, yes, no)
    rows = [
        json.loads(line)
        for line in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    types = {r["fact_type"] for r in rows}
    assert "paired_binary_survivor_resolved_without_oms_exit" in types
    assert "paired_binary_resolution_exit_accounting" in types
    pnl_row = next(r for r in rows if r["fact_type"] == "paired_binary_realized_pnl_unavailable")
    assert pnl_row["payload"]["reason"] == "resolution_cashflow_missing"
    assert pnl_row["payload"]["terminal_reason"] == "market_resolution_without_oms_exit"
    assert pnl_row["payload"]["manual_reconciliation_required"] is True


def test_analysis_terminal_reason_from_facts() -> None:
    from scripts.analyze_live_runs_review import terminal_exit_summary

    rows = [
        {
            "fact_type": "paired_binary_resolution_exit_accounting",
            "payload": {
                "terminal_reason": "market_resolution_without_oms_exit",
                "pnl_status": "resolution_cashflow_missing",
            },
        },
        {
            "fact_type": "paired_binary_realized_pnl_unavailable",
            "payload": {"reason": "resolution_cashflow_missing"},
        },
    ]
    summary = terminal_exit_summary(rows)
    assert summary["terminal_reason"] == "market_resolution_without_oms_exit"
    assert summary["pnl_status"] == "resolution_cashflow_missing"
