"""Paired-binary PnL emission with fill reconciliation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import TradeFillRecord
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_OMS_FILL_DISCREPANCY_DETECTED,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.cashflows import SOURCE_OMS_MATCH_EVIDENCE
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.facts import emit_realized_pnl
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _leg_book(tid: str = YES) -> LegBook:
    return LegBook(TokenId(tid), Decimal("0.50"), Decimal("0.51"), False)


def _state() -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.DONE,
        effective_qty=Decimal("5"),
        yes_token_id=YES,
        no_token_id=NO,
        pair_correlation_id="pair-1",
    )
    q = Decimal("5")
    state.yes.entry_cash = Decimal("2.40")
    state.yes.entry_qty = q
    state.yes.entry_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.no.entry_cash = Decimal("2.65")
    state.no.entry_qty = q
    state.no.entry_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.yes.exit_cash = Decimal("3.95")
    state.yes.exit_qty = q
    state.yes.exit_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.no.exit_cash = Decimal("1.20")
    state.no.exit_qty = q
    state.no.exit_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    return state


def _coord_pm_ui_fills() -> RuntimeCoordinator:
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
    )
    now = datetime.now(timezone.utc)
    fills = (
        (YES, Side.BUY, "0.497"),
        (NO, Side.BUY, "0.547"),
        (NO, Side.SELL, "0.227"),
        (YES, Side.SELL, "0.778"),
    )
    for token, side, px in fills:
        coord.wallet.record_user_ws_trade(
            TradeFillRecord(
                token_id=TokenId(token),
                side=side,
                size=Decimal("5"),
                price=Decimal(px),
                status="CONFIRMED",
                ts_utc=now,
            )
        )
    return coord


def test_oms_only_emits_tentative_not_final(tmp_path: Path) -> None:
    path = tmp_path / "facts.jsonl"
    with JsonlSink(path) as sink:
        emit_realized_pnl(sink, RunId("r1"), _state(), _leg_book(YES), _leg_book(NO))
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    types = {r["fact_type"] for r in rows}
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL not in types
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE in types
    tentative = next(r for r in rows if r["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE)
    assert tentative["payload"]["pnl_status"] == "tentative"
    assert tentative["payload"]["pnl_total"] == "0.10"


def test_ws_confirmed_emits_final_pnl(tmp_path: Path) -> None:
    state = _state()
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
    )
    now = datetime.now(timezone.utc)
    for token, side, cash in (
        (YES, Side.BUY, state.yes.entry_cash),
        (NO, Side.BUY, state.no.entry_cash),
        (YES, Side.SELL, state.yes.exit_cash),
        (NO, Side.SELL, state.no.exit_cash),
    ):
        coord.wallet.record_user_ws_trade(
            TradeFillRecord(
                token_id=TokenId(token),
                side=side,
                size=Decimal("5"),
                price=cash / Decimal("5"),  # type: ignore[operator]
                status="CONFIRMED",
                ts_utc=now,
            )
        )
    path = tmp_path / "facts.jsonl"
    with JsonlSink(path) as sink:
        emit_realized_pnl(sink, RunId("r2"), state, _leg_book(YES), _leg_book(NO), coord=coord)
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    types = {r["fact_type"] for r in rows}
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL in types
    pnl = next(r for r in rows if r["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL)
    assert pnl["payload"]["pnl_status"] == "final"


def test_discrepancy_fact_emitted(tmp_path: Path) -> None:
    path = tmp_path / "facts.jsonl"
    with JsonlSink(path) as sink:
        emit_realized_pnl(
            sink,
            RunId("r3"),
            _state(),
            _leg_book(YES),
            _leg_book(NO),
            coord=_coord_pm_ui_fills(),
        )
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert FACT_TYPE_OMS_FILL_DISCREPANCY_DETECTED in {r["fact_type"] for r in rows}
    tentative = next(r for r in rows if r["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_TENTATIVE)
    assert abs(Decimal(tentative["payload"]["pnl_total"]) - Decimal("-0.195")) < Decimal("0.01")
