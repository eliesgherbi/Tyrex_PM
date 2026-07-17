"""R8: inventory terminal FLAT vs FLAT_WITH_DUST consistency."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.execution.polymarket.lifecycle_exit_plan import book_from_clob_levels
from tyrex_pm.execution.polymarket.mutation_transport import SpyMutationTransport
from tyrex_pm.execution.polymarket.settlement import FakeSettlementClock, TradeEvidence, TradeSettlementStatus
from tyrex_pm.runtime.r7_lifecycle_policy import (
    EMERGENCY_EXIT_PRICE_FLOOR,
    MAX_EXIT_SLIPPAGE_FROM_TOUCH,
    NORMAL_EXIT_PRICE_FLOOR,
    default_exit_price_policy,
)
from tyrex_pm.execution.polymarket.lifecycle_exit_plan import (
    ExitPlanStatus,
    ExitUrgency,
    plan_lifecycle_fak_sell,
)
from tyrex_pm.runtime.r7b_live_once import TerminalOutcome, run_r7b_live_once
from test_r7b_live_once import _base_args


NOW = datetime(2026, 7, 17, 12, 1, 0, tzinfo=timezone.utc)


def test_dust_residual_terminal_is_flat_with_dust_not_flat(tmp_path: Path) -> None:
    """Replay 55fd9a76 shape: sell 9.47 of 9.470587 → dust 0.000587 → FLAT_WITH_DUST."""
    spy = SpyMutationTransport()
    acquired = Decimal("9.470587")
    sold = Decimal("9.47")

    def bal_poll():
        # After SELL spy does not reduce balance; accounting uses confirmed_sold.
        return acquired, acquired

    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [
                TradeEvidence(
                    trade_id="buy1",
                    order_id="0xspy0001",
                    side="BUY",
                    size=acquired,
                    price=Decimal("0.51"),
                    status=TradeSettlementStatus.CONFIRMED,
                )
            ],
            settlement_balance_poller=bal_poll,
            settlement_clock=FakeSettlementClock(),
            exit_book_provider=lambda _t: book_from_clob_levels(
                token_id="t",
                bids=[{"price": "0.50", "size": "100"}],
                asks=[{"price": "0.51", "size": "100"}],
                ts_event=NOW,
            ),
            exit_now_provider=lambda: NOW,
            exit_price_policy=default_exit_price_policy(),
        )
    )
    # quantize sell to 9.47; residual accounting 0.000587
    assert result.outcome is TerminalOutcome.FLAT_WITH_DUST
    assert result.exit_code == 0
    assert result.report["final"]["inventory_terminal"] == "FLAT_WITH_DUST"
    assert result.report["final"]["lifecycle_completed"] is True
    assert Decimal(result.report["final"]["residual_quantity"]) == Decimal("0.000587")
    assert result.report["terminal"] == "FLAT_WITH_DUST"


def test_exact_zero_residual_is_flat(tmp_path: Path) -> None:
    spy = SpyMutationTransport()
    qty = Decimal("9.00")
    result = run_r7b_live_once(
        _base_args(
            tmp_path,
            execute_live=True,
            mutation_transport=spy,
            settlement_trade_poller=lambda: [
                TradeEvidence(
                    trade_id="buy1",
                    order_id="0xspy0001",
                    side="BUY",
                    size=qty,
                    price=Decimal("0.55"),
                    status=TradeSettlementStatus.CONFIRMED,
                )
            ],
            settlement_balance_poller=lambda: (qty, qty),
            settlement_clock=FakeSettlementClock(),
            exit_book_provider=lambda _t: book_from_clob_levels(
                token_id="t",
                bids=[{"price": "0.54", "size": "100"}],
                asks=[{"price": "0.55", "size": "100"}],
                ts_event=NOW,
            ),
            exit_now_provider=lambda: NOW,
        )
    )
    # sell qty quantized to 9.00 exactly
    assert result.outcome is TerminalOutcome.FLAT
    assert result.report["final"]["inventory_terminal"] == "FLAT"


def test_exit_floor_formulas_normal_vs_emergency() -> None:
    """Absolute floors identical; NORMAL alone enforces touch-slippage."""
    assert NORMAL_EXIT_PRICE_FLOOR == EMERGENCY_EXIT_PRICE_FLOOR == Decimal("0.01")
    assert MAX_EXIT_SLIPPAGE_FROM_TOUCH == Decimal("0.05")
    book = book_from_clob_levels(
        token_id="t",
        bids=[{"price": "0.50", "size": "1"}, {"price": "0.40", "size": "20"}],
        asks=[{"price": "0.51", "size": "100"}],
        ts_event=NOW,
    )
    normal = plan_lifecycle_fak_sell(
        book=book,
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=default_exit_price_policy(),
        urgency=ExitUrgency.NORMAL,
    )
    assert normal.status is ExitPlanStatus.REFUSE_SLIPPAGE
    emergency = plan_lifecycle_fak_sell(
        book=book,
        quantity=Decimal("5"),
        tick_size=Decimal("0.01"),
        now=NOW,
        policy=default_exit_price_policy(),
        urgency=ExitUrgency.EMERGENCY,
    )
    # Emergency skips touch-slippage; still depth-aware + absolute floor + fresh book
    assert emergency.ok
    assert emergency.limit_price == Decimal("0.40")
    assert emergency.limit_price >= EMERGENCY_EXIT_PRICE_FLOOR


def test_successful_live_fixture_replay_semantics() -> None:
    """Fixture report 55fd9a76 must classify as FLAT_WITH_DUST under R8 rules."""
    import json
    from pathlib import Path

    incident = Path("tests/fixtures/r7d2_second_live_76e8470a_report.json")
    success = Path("tests/fixtures/r7_success_55fd9a76_report.json")
    assert incident.exists()
    rep = json.loads(success.read_text(encoding="utf-8"))
    assert rep["run_id"].startswith("55fd9a76")
    assert Decimal(rep["final"]["residual_quantity"]) == Decimal("0.000587")
    assert rep["final"]["inventory_terminal"] == "FLAT_WITH_DUST"
    assert rep["final"]["realized_result"] == "FLAT_WITH_DUST"
    assert rep["r8_corrected_terminal"] == "FLAT_WITH_DUST"
    assert rep["sell"]["entry_buy_limit_not_used"] is True
    assert rep["sell"]["book_fingerprint"] == "0c830ec2fbf3f0a6"
    assert Decimal(rep["sell"]["limit"]) == Decimal("0.5")
    assert Decimal(rep["buy"]["limit"]) == Decimal("0.51")
