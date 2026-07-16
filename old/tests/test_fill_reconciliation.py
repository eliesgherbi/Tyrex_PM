"""Fill reconciliation layer unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import TradeFillRecord
from tyrex_pm.execution.fill_reconciliation import (
    FillConfidence,
    FillReconciliationConfig,
    FillSource,
    compute_reconciled_pnl,
    manual_cashflows_from_fills,
    reconcile_paired_binary_cashflows,
)
from tyrex_pm.runtime.cashflows import SOURCE_OMS_MATCH_EVIDENCE
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _state_oms_only() -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.DONE,
        effective_qty=Decimal("5"),
        yes_token_id=YES,
        no_token_id=NO,
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


def _coord_with_confirmed(state: PairedBinaryRuntimeState) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
    )
    now = datetime.now(timezone.utc)

    def _rec(token: str, side: Side, cash: Decimal, qty: Decimal) -> None:
        coord.wallet.record_user_ws_trade(
            TradeFillRecord(
                token_id=TokenId(token),
                side=side,
                size=qty,
                price=cash / qty,
                status="CONFIRMED",
                ts_utc=now,
            )
        )

    q = state.yes.entry_qty or Decimal("5")
    _rec(YES, Side.BUY, state.yes.entry_cash, q)  # type: ignore[arg-type]
    _rec(NO, Side.BUY, state.no.entry_cash, q)  # type: ignore[arg-type]
    _rec(YES, Side.SELL, state.yes.exit_cash, q)  # type: ignore[arg-type]
    _rec(NO, Side.SELL, state.no.exit_cash, q)  # type: ignore[arg-type]
    return coord


def test_oms_ack_alone_produces_tentative_pnl() -> None:
    state = _state_oms_only()
    cashflows = reconcile_paired_binary_cashflows(state)
    result = compute_reconciled_pnl(cashflows, effective_qty=Decimal("5"))
    assert result.status == "tentative"
    assert result.pnl_total == Decimal("0.10")
    assert not cashflows.all_final
    assert cashflows.entry_yes is not None
    assert cashflows.entry_yes.source == FillSource.OMS_ACK
    assert cashflows.entry_yes.confidence == FillConfidence.TENTATIVE


def test_confirmed_ws_produces_final_pnl() -> None:
    state = _state_oms_only()
    coord = _coord_with_confirmed(state)
    cashflows = reconcile_paired_binary_cashflows(state, coord=coord)
    result = compute_reconciled_pnl(cashflows, effective_qty=Decimal("5"))
    assert result.status == "final"
    assert cashflows.all_final
    assert cashflows.entry_yes is not None
    assert cashflows.entry_yes.source == FillSource.VENUE_RECONCILED


def test_discrepancy_when_oms_differs_from_ws() -> None:
    state = _state_oms_only()
    coord = _coord_with_confirmed(state)
    # WS fills at PM UI prices (different from OMS)
    pm_prices = {
        YES: (Decimal("0.497"), Side.BUY),
        NO: (Decimal("0.547"), Side.BUY),
    }
    now = datetime.now(timezone.utc)
    coord.wallet.trade_fill_records.clear()
    for token, (px, side) in pm_prices.items():
        coord.wallet.record_user_ws_trade(
            TradeFillRecord(
                token_id=TokenId(token),
                side=side,
                size=Decimal("5"),
                price=px,
                status="CONFIRMED",
                ts_utc=now,
            )
        )
    coord.wallet.record_user_ws_trade(
        TradeFillRecord(
            token_id=TokenId(NO),
            side=Side.SELL,
            size=Decimal("5"),
            price=Decimal("0.227"),
            status="CONFIRMED",
            ts_utc=now,
        )
    )
    coord.wallet.record_user_ws_trade(
        TradeFillRecord(
            token_id=TokenId(YES),
            side=Side.SELL,
            size=Decimal("5"),
            price=Decimal("0.778"),
            status="CONFIRMED",
            ts_utc=now,
        )
    )
    cashflows = reconcile_paired_binary_cashflows(state, coord=coord)
    assert cashflows.has_discrepancy
    result = compute_reconciled_pnl(cashflows, effective_qty=Decimal("5"))
    assert result.status == "tentative"
    assert result.pnl_total is not None
    assert abs(result.pnl_total - Decimal("-0.195")) < Decimal("0.01")


def test_manual_pm_ui_pnl_example() -> None:
    manual = {
        "yes_buy": {"price": "0.497", "qty": "5"},
        "no_buy": {"price": "0.547", "qty": "5"},
        "no_sell": {"price": "0.227", "qty": "5"},
        "yes_sell": {"price": "0.778", "qty": "5"},
    }
    cashflows = manual_cashflows_from_fills(manual)
    result = compute_reconciled_pnl(cashflows, effective_qty=Decimal("5"))
    assert result.status == "final"
    assert result.pnl_total is not None
    assert abs(result.pnl_total - Decimal("-0.195")) < Decimal("0.01")


def test_missing_cashflows_unavailable() -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE, effective_qty=Decimal("5"))
    cashflows = reconcile_paired_binary_cashflows(state)
    result = compute_reconciled_pnl(cashflows, effective_qty=Decimal("5"))
    assert result.status == "unavailable"
    assert result.pnl_total is None
