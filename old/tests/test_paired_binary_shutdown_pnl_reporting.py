"""Shutdown completion reporting must emit PnL or explicit unavailable reason."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_DONE,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.cashflows import SOURCE_OMS_MATCH_EVIDENCE
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.facts import emit_shutdown_completion_reporting
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from paired_binary_shutdown_helpers import NO, YES


def _leg_book(tid: str = YES) -> LegBook:
    from tyrex_pm.core.ids import TokenId

    return LegBook(TokenId(tid), Decimal("0.48"), Decimal("0.49"), False)


def _state_complete_cashflows(*, yes_exit: str = "2.75", no_exit: str = "2.60") -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.DONE,
        effective_qty=Decimal("5"),
        yes_token_id=YES,
        no_token_id=NO,
        pair_correlation_id="pair-pnl",
    )
    q = Decimal("5")
    state.yes.entry_cash = Decimal("2.50")
    state.yes.entry_qty = q
    state.yes.entry_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.no.entry_cash = Decimal("2.55")
    state.no.entry_qty = q
    state.no.entry_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.yes.exit_cash = Decimal(yes_exit)
    state.yes.exit_qty = q
    state.yes.exit_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.no.exit_cash = Decimal(no_exit)
    state.no.exit_qty = q
    state.no.exit_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    return state


def test_shutdown_completion_emits_realized_pnl(tmp_path: Path) -> None:
    state = _state_complete_cashflows()
    path = tmp_path / "facts.jsonl"
    with JsonlSink(path) as sink:
        emit_shutdown_completion_reporting(
            sink,
            RunId("pnl-ok"),
            state,
            _leg_book(YES),
            _leg_book(NO),
            decision_id="dec-shutdown-1",
        )
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    types = {r["fact_type"] for r in rows}
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE in types
    assert FACT_TYPE_PAIRED_BINARY_DONE in types
    pnl = next(r for r in rows if r["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE)
    assert pnl["payload"]["pnl_total"] == "0.30"
    assert pnl["payload"]["pnl_status"] == "tentative"
    done = next(r for r in rows if r["fact_type"] == FACT_TYPE_PAIRED_BINARY_DONE)
    assert done["payload"]["completion_reason"] == "shutdown_force_flatten"


def test_shutdown_completion_emits_pnl_unavailable_when_incomplete(tmp_path: Path) -> None:
    state = _state_complete_cashflows()
    state.yes.exit_cash = None
    state.yes.exit_qty = None
    path = tmp_path / "facts.jsonl"
    with JsonlSink(path) as sink:
        emit_shutdown_completion_reporting(
            sink,
            RunId("pnl-missing"),
            state,
            _leg_book(YES),
            _leg_book(NO),
        )
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    types = {r["fact_type"] for r in rows}
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE in types
    assert FACT_TYPE_PAIRED_BINARY_DONE in types
    unavailable = next(
        r for r in rows if r["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE
    )
    assert unavailable["payload"]["reason"] == "missing_exit_cashflow"
    assert "yes_exit_cash" in unavailable["payload"]["missing_fields"]
