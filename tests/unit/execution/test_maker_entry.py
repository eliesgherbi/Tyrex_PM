"""G6 maker entry POST: live GTC returns without waiting for a fill."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, LiquidityRole, new_intent_id
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.run_config import load_trading_run_config

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


class _MakerVenue:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.cancel_all_called = False

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def post_order(self, prepared):  # noqa: ANN001
        self.posts.append(prepared.payload.side.value)
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="maker-1",
            status="live",
            cumulative_matched_shares=Decimal("0"),
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        del order_ids
        raise AssertionError("maker live POST should not cancel immediately")

    async def cancel_all(self) -> None:
        self.cancel_all_called = True

    async def list_open_orders(self, **_kwargs):
        return ({"id": "maker-1"},)

    async def list_account_trades(self, **_kwargs):
        return ()

    async def conditional_balance(self, _token_id):
        return Decimal("0"), Decimal("100")

    async def close(self):
        return None


def _maker_enter() -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        reason_code="ENTRY_ASK_HIT",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.01"),
        liquidity_role=LiquidityRole.MAKER,
    )


@pytest.mark.asyncio
async def test_maker_live_post_returns_without_fill_wait() -> None:
    venue = _MakerVenue()
    lifecycle = ExecutionLifecycle(
        coordinator=AccountExecutionCoordinator(
            journal=MemoryExecutionJournal(),
            gateway=venue,
            final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
        ),
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=CapabilityController(live_requested=True),
        policy=LifecyclePolicy(evidence_timeout_s=30),
    )
    await lifecycle.open(
        session_id="session",
        strategy_id="ask70",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    state = await lifecycle.submit_entry(_maker_enter(), token_id="token")
    assert venue.posts == ["BUY"]
    assert venue.cancel_all_called is False
    assert state.working_maker_entry is True
    assert state.phase is ExecutionPhase.POSITION_PENDING_CONFIRMATION
    assert state.terminal is False
    await lifecycle.coordinator.close()


def test_ask70_default_stays_taker_and_maker_yaml_is_explicit() -> None:
    default = load_trading_run_config(Path("config/runs/ask70_protection_tiny_live.yaml"))
    assert default.strategy.liquidity_role == "taker"
    maker = load_trading_run_config(Path("config/runs/ask70_maker_tiny_live_danger.yaml"))
    assert maker.strategy.liquidity_role == "maker"
    assert maker.strategy.maker_limit_price == Decimal("0.01")
