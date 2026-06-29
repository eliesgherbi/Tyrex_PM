"""Cashflow-based realized PnL for paired binary."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_PAIRED_BINARY_PRICE_BASED_PNL_ESTIMATE,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL,
    FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.cashflows import (
    SOURCE_OMS_MATCH_EVIDENCE,
    extract_matched_cashflow,
)
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook
from tyrex_pm.strategies.paired_binary.entry_price import (
    SOURCE_OMS_LIMIT_PRICE_FALLBACK,
    resolve_leg_entry_price,
)
from tyrex_pm.strategies.paired_binary.facts import (
    emit_price_based_pnl_estimate,
    emit_realized_pnl,
    emit_realized_pnl_unavailable,
)
from tyrex_pm.strategies.paired_binary.pnl import (
    price_based_pnl_estimate_from_stored_prices,
    realized_pnl_from_cashflows,
)
from tyrex_pm.strategies.paired_binary.state import (
    LegRuntime,
    PairedBinaryPhase,
    PairedBinaryRuntimeState,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"


def _leg_book(tid: str = YES) -> LegBook:
    from tyrex_pm.core.ids import TokenId

    return LegBook(TokenId(tid), Decimal("0.50"), Decimal("0.51"), False)


def _state_with_cashflows(
    *,
    yes_entry_cash: str,
    no_entry_cash: str,
    yes_exit_cash: str,
    no_exit_cash: str,
    qty: str = "5",
    yes_exit_bid: str | None = "0.55",
) -> PairedBinaryRuntimeState:
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.DONE,
        effective_qty=Decimal(qty),
        yes_entry=Decimal("0.53"),
        no_entry=Decimal("0.48"),
        pair_correlation_id="pair-1",
    )
    q = Decimal(qty)
    state.yes.entry_cash = Decimal(yes_entry_cash)
    state.yes.entry_qty = q
    state.yes.entry_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.no.entry_cash = Decimal(no_entry_cash)
    state.no.entry_qty = q
    state.no.entry_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.yes.exit_cash = Decimal(yes_exit_cash)
    state.yes.exit_qty = q
    state.yes.exit_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    state.no.exit_cash = Decimal(no_exit_cash)
    state.no.exit_qty = q
    state.no.exit_cash_source = SOURCE_OMS_MATCH_EVIDENCE
    if yes_exit_bid is not None:
        state.yes.last_exit_bid = Decimal(yes_exit_bid)
    return state


def _read_facts(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_realized_pnl_uses_cashflows_not_trigger_prices() -> None:
    state = _state_with_cashflows(
        yes_entry_cash="2.65",
        no_entry_cash="2.40",
        yes_exit_cash="2.85",
        no_exit_cash="1.85",
        yes_exit_bid="0.55",
    )
    result = realized_pnl_from_cashflows(state)
    assert result is not None
    assert result.pnl_total == Decimal("-0.35")
    assert result.pnl_per_pair == Decimal("-0.07")
    assert result.pnl_total != Decimal("-0.45")


def test_realized_pnl_matches_oms_buy_sell_amounts() -> None:
    buy_yes = extract_matched_cashflow(
        Side.BUY,
        {"status": "matched", "makingAmount": "2.65", "takingAmount": "5"},
    )
    buy_no = extract_matched_cashflow(
        Side.BUY,
        {"status": "matched", "makingAmount": "2.40", "takingAmount": "5"},
    )
    sell_no = extract_matched_cashflow(
        Side.SELL,
        {"status": "matched", "takingAmount": "1.85", "makingAmount": "5"},
    )
    sell_yes = extract_matched_cashflow(
        Side.SELL,
        {"status": "matched", "takingAmount": "2.85", "makingAmount": "5"},
    )
    assert buy_yes is not None and buy_no is not None
    assert sell_no is not None and sell_yes is not None
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE, effective_qty=Decimal("5"))
    state.yes.entry_cash = buy_yes.cash
    state.yes.entry_qty = buy_yes.qty
    state.yes.entry_cash_source = buy_yes.source
    state.no.entry_cash = buy_no.cash
    state.no.entry_qty = buy_no.qty
    state.no.entry_cash_source = buy_no.source
    state.yes.exit_cash = sell_yes.cash
    state.yes.exit_qty = sell_yes.qty
    state.yes.exit_cash_source = sell_yes.source
    state.no.exit_cash = sell_no.cash
    state.no.exit_qty = sell_no.qty
    state.no.exit_cash_source = sell_no.source
    result = realized_pnl_from_cashflows(state)
    assert result is not None
    assert result.buy_cash_total == Decimal("5.05")
    assert result.sell_cash_total == Decimal("4.70")
    assert result.pnl_total == Decimal("-0.35")


def test_realized_pnl_handles_price_improvement() -> None:
    state = _state_with_cashflows(
        yes_entry_cash="2.74",
        no_entry_cash="2.49",
        yes_exit_cash="2.76",
        no_exit_cash="1.77",
        yes_exit_bid=None,
    )
    result = realized_pnl_from_cashflows(state)
    assert result is not None
    assert result.pnl_total == Decimal("-0.70")
    assert result.pnl_per_pair == Decimal("-0.14")


def test_realized_pnl_handles_partial_fills() -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE, effective_qty=Decimal("3"))
    state.yes.entry_cash = Decimal("1.59")
    state.yes.entry_qty = Decimal("3")
    state.no.entry_cash = Decimal("1.44")
    state.no.entry_qty = Decimal("3")
    state.yes.exit_cash = Decimal("1.71")
    state.yes.exit_qty = Decimal("3")
    state.no.exit_cash = Decimal("1.11")
    state.no.exit_qty = Decimal("3")
    result = realized_pnl_from_cashflows(state)
    assert result is not None
    assert result.pnl_total == Decimal("-0.21")
    assert result.pnl_per_pair == Decimal("-0.07")


def test_realized_pnl_handles_different_entry_exit_qty() -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE, effective_qty=Decimal("4"))
    state.yes.entry_cash = Decimal("2.12")
    state.yes.entry_qty = Decimal("4")
    state.no.entry_cash = Decimal("1.92")
    state.no.entry_qty = Decimal("4")
    state.yes.exit_cash = Decimal("2.28")
    state.yes.exit_qty = Decimal("4")
    state.no.exit_cash = Decimal("1.48")
    state.no.exit_qty = Decimal("4")
    result = realized_pnl_from_cashflows(state)
    assert result is not None
    assert result.effective_pair_qty == Decimal("4")
    assert result.pnl_total == Decimal("-0.28")


def test_realized_pnl_fact_includes_cashflow_sources(tmp_path: Path) -> None:
    state = _state_with_cashflows(
        yes_entry_cash="2.65",
        no_entry_cash="2.40",
        yes_exit_cash="2.85",
        no_exit_cash="1.85",
    )
    sink_path = tmp_path / "facts.jsonl"
    with JsonlSink(sink_path) as sink:
        emit_realized_pnl(sink, RunId("run-1"), state, _leg_book(YES), _leg_book(NO))
    facts = _read_facts(sink_path)
    assert len(facts) == 1
    assert facts[0]["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL
    payload = facts[0]["payload"]
    assert payload["yes_entry_cash_source"] == SOURCE_OMS_MATCH_EVIDENCE
    assert payload["pnl_total"] == "-0.35"
    assert payload["yes_exit_avg_price"] == "0.57"
    assert payload["no_exit_avg_price"] == "0.37"


def test_realized_pnl_unavailable_when_cashflow_missing(tmp_path: Path) -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.DONE, effective_qty=Decimal("5"))
    state.yes.entry_cash = Decimal("2.65")
    state.yes.entry_qty = Decimal("5")
    state.no.entry_cash = Decimal("2.40")
    state.no.entry_qty = Decimal("5")
    sink_path = tmp_path / "facts.jsonl"
    with JsonlSink(sink_path) as sink:
        emit_realized_pnl(sink, RunId("run-1"), state, _leg_book(YES), _leg_book(NO))
    facts = _read_facts(sink_path)
    types = [f["fact_type"] for f in facts]
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE in types
    assert FACT_TYPE_PAIRED_BINARY_REALIZED_PNL not in types
    unavailable = next(f for f in facts if f["fact_type"] == FACT_TYPE_PAIRED_BINARY_REALIZED_PNL_UNAVAILABLE)
    assert "yes_exit_cash" in unavailable["payload"]["missing_fields"]


def test_price_based_estimate_is_non_authoritative(tmp_path: Path) -> None:
    state = _state_with_cashflows(
        yes_entry_cash="2.65",
        no_entry_cash="2.40",
        yes_exit_cash="2.85",
        no_exit_cash="1.85",
        yes_exit_bid="0.55",
    )
    state.yes.exit_cash = None
    state.yes.exit_qty = None
    sink_path = tmp_path / "facts.jsonl"
    with JsonlSink(sink_path) as sink:
        emit_price_based_pnl_estimate(sink, RunId("run-1"), state, _leg_book(YES), _leg_book(NO))
    facts = _read_facts(sink_path)
    assert facts[0]["fact_type"] == FACT_TYPE_PAIRED_BINARY_PRICE_BASED_PNL_ESTIMATE
    assert facts[0]["payload"]["authoritative"] is False


def test_oms_matched_avg_not_limit_price() -> None:
    from tyrex_pm.core.ids import ClientOrderId, TokenId
    from tyrex_pm.state.order_store import LocalOrder

    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
    )
    cid = ClientOrderId("buy-yes-1")
    coord.orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=None,
        token_id=TokenId(YES),
        side=Side.BUY,
        remaining=Decimal("0"),
        original_size=Decimal("5"),
        size_matched=Decimal("5"),
        ack_status="matched",
        limit_price=Decimal("0.55"),
    )
    leg = LegRuntime(
        entry_cash=Decimal("2.65"),
        entry_qty=Decimal("5"),
        entry_cash_source=SOURCE_OMS_MATCH_EVIDENCE,
    )
    res = resolve_leg_entry_price(coord, TokenId(YES), client_order_id=str(cid), leg_runtime=leg)
    assert res.price == Decimal("0.53")
    assert res.source == SOURCE_OMS_MATCH_EVIDENCE

    res_limit = resolve_leg_entry_price(coord, TokenId(YES), client_order_id=str(cid))
    assert res_limit.price == Decimal("0.55")
    assert res_limit.source == SOURCE_OMS_LIMIT_PRICE_FALLBACK


def test_run_1782733789_regression() -> None:
    state = _state_with_cashflows(
        yes_entry_cash="2.65",
        no_entry_cash="2.40",
        yes_exit_cash="2.85",
        no_exit_cash="1.85",
        yes_exit_bid="0.55",
    )
    result = realized_pnl_from_cashflows(state)
    assert result is not None
    assert result.pnl_total == Decimal("-0.35")
    stale = price_based_pnl_estimate_from_stored_prices(
        yes_entry=Decimal("0.53"),
        no_entry=Decimal("0.48"),
        yes_exit=Decimal("0.55"),
        no_exit=Decimal("0.37"),
        qty=Decimal("5"),
    )
    assert stale is not None
    assert stale[3] == Decimal("-0.45")
