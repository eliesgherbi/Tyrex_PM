"""G4 cancel path: session-owned ids only, never cancel_all."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, OrderId, StrategyId
from tyrex_pm.core.intents import CancelIntent, ExitIntent, new_intent_id
from tyrex_pm.execution.cancel import cancel_result_from_sdk
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import (
    CancelObserved,
    ExecutionRole,
    ReconciliationObserved,
    evidence_from_dict,
    evidence_to_dict,
    new_envelope,
)
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.strategies.decisions import IntentLike

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


class _CancelVenue:
    def __init__(self, *, already_matched: bool = False) -> None:
        self.already_matched = already_matched
        self.canceled: list[tuple[str, ...]] = []
        self.cancel_all_called = False
        self.posts: list[str] = []
        self.balance = Decimal("7")
        self.open_ids = ("gtc-1",)

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def post_order(self, prepared):  # noqa: ANN001
        self.posts.append(prepared.payload.side.value)
        self.balance = Decimal("0")
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="venue-exit",
            status="matched",
            cumulative_matched_shares=prepared.payload.shares,
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        ids = tuple(order_ids)
        self.canceled.append(ids)
        if self.already_matched:
            return cancel_result_from_sdk(
                {"canceled": (), "not_canceled": {ids[0]: "Order already matched"}}
            )
        self.open_ids = ()
        return cancel_result_from_sdk({"canceled": ids, "not_canceled": {}})

    async def cancel_all(self) -> None:
        self.cancel_all_called = True

    async def list_open_orders(self, **_kwargs):
        return tuple({"id": item} for item in self.open_ids)

    async def list_account_trades(self, **_kwargs):
        return ()

    async def conditional_balance(self, _token_id):
        return self.balance, Decimal("100")

    async def close(self):
        return None


def _lifecycle(venue: _CancelVenue) -> ExecutionLifecycle:
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=venue,
        final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
    )
    return ExecutionLifecycle(
        coordinator=coordinator,
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=CapabilityController(live_requested=True, selected_token_sellable=True),
        policy=LifecyclePolicy(
            evidence_timeout_s=1,
            reconciliation_interval_s=0.001,
            exit_retry_limit=2,
            exit_retry_budget_s=1,
            cancel_delay_s=0.001,
        ),
    )


async def _open_with_resting(lifecycle: ExecutionLifecycle) -> None:
    await lifecycle.open(
        session_id="session",
        strategy_id="ask70",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    await lifecycle.coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="session", dedupe_key="recon-resting"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=("gtc-1",),
        )
    )


def test_cancel_intent_is_an_intent_like() -> None:
    intent = CancelIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        order_id=OrderId("gtc-1"),
        created_at=NOW,
        correlation_id=CorrelationId("c1"),
        causation_id=None,
        reason_code="SOFT_OCO",
    )
    assert isinstance(intent, CancelIntent)
    accepted: IntentLike = intent
    assert accepted.reason_code == "SOFT_OCO"


def test_already_matched_is_inventory_not_empty_success() -> None:
    result = cancel_result_from_sdk(
        {"canceled": (), "not_canceled": {"gtc-1": "Order already matched"}}
    )
    assert result.canceled == ()
    assert result.matched_instead == ("gtc-1",)
    assert result.delay_pending == ()


@pytest.mark.asyncio
async def test_cancel_success_empties_open_ids_before_flatten() -> None:
    venue = _CancelVenue()
    lifecycle = _lifecycle(venue)
    await _open_with_resting(lifecycle)
    assert lifecycle.state is not None
    assert lifecycle.state.open_venue_order_ids == ("gtc-1",)
    state = await lifecycle.submit_exit(
        ExitIntent(
            intent_id=new_intent_id(),
            strategy_id=StrategyId("ask70"),
            instrument_id=InstrumentId("yes"),
            market_id=MarketId("market"),
            created_at=NOW,
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            reason_code="PROTECTION_SL",
        ),
        token_id="token",
        min_order_size=Decimal("1"),
        last_bid=Decimal("0.55"),
    )
    assert venue.canceled == [("gtc-1",)]
    assert venue.cancel_all_called is False
    assert "SELL" in venue.posts
    assert state.open_venue_order_ids == ()
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_already_matched_cancel_does_not_call_cancel_all() -> None:
    venue = _CancelVenue(already_matched=True)
    venue.open_ids = ()
    lifecycle = _lifecycle(venue)
    await _open_with_resting(lifecycle)
    await lifecycle.submit_exit(
        ExitIntent(
            intent_id=new_intent_id(),
            strategy_id=StrategyId("ask70"),
            instrument_id=InstrumentId("yes"),
            market_id=MarketId("market"),
            created_at=NOW,
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            reason_code="PROTECTION_SL",
        ),
        token_id="token",
        min_order_size=Decimal("1"),
        last_bid=Decimal("0.55"),
    )
    assert venue.cancel_all_called is False
    assert venue.canceled == [("gtc-1",)]
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_cancel_intent_is_consumed() -> None:
    venue = _CancelVenue()
    lifecycle = _lifecycle(venue)
    await _open_with_resting(lifecycle)
    state = await lifecycle.cancel_intent(
        CancelIntent(
            intent_id=new_intent_id(),
            strategy_id=StrategyId("ask70"),
            order_id=OrderId("gtc-1"),
            created_at=NOW,
            correlation_id=CorrelationId("c1"),
            causation_id=None,
            reason_code="OPERATOR_CANCEL",
        )
    )
    assert venue.canceled == [("gtc-1",)]
    assert venue.cancel_all_called is False
    assert "gtc-1" not in state.open_venue_order_ids
    await lifecycle.coordinator.close()


def test_cancel_observed_roundtrip() -> None:
    event = CancelObserved(
        **new_envelope(session_id="s1", dedupe_key="cancel"),
        canceled=("gtc-1",),
        not_canceled=(("gtc-2", "Order already matched"),),
        matched_instead=("gtc-2",),
    )
    restored = evidence_from_dict(evidence_to_dict(event))
    assert restored.canceled == ("gtc-1",)
    assert restored.matched_instead == ("gtc-2",)


def test_live_unfilled_limit_exit_stays_position_open() -> None:
    from tyrex_pm.execution.evidence import (
        DispatchAuthorized,
        OrderRequested,
        SessionOpened,
        SubmissionAttempted,
        SubmissionResponseObserved,
    )
    from tyrex_pm.execution.orders import LimitOrderSpec, OrderSide, TimeInForce
    from tyrex_pm.execution.reducer import reduce_execution_event
    from tyrex_pm.execution.session_state import ExecutionSessionState

    state = ExecutionSessionState()
    reduce_execution_event(
        state,
        SessionOpened(
            **new_envelope(session_id="s1", dedupe_key="open"),
            strategy_id="ask70",
            market_id="m",
            window_id="w",
            token_id="t",
        ),
    )
    state.phase = ExecutionPhase.POSITION_OPEN
    state.confirmed_position_shares = Decimal("7")
    spec = LimitOrderSpec(
        order_id="resting-tp",
        market_id="m",
        instrument_id="m:YES",
        token_id="t",
        side=OrderSide.SELL,
        shares=Decimal("7"),
        limit_price=Decimal("0.85"),
        time_in_force=TimeInForce.GTC,
        metadata={"post_only": True, "candidate_monotonic_ns": 1},
    )
    reduce_execution_event(
        state,
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="req"),
            role=ExecutionRole.EXIT,
            order=spec,
        ),
    )
    reduce_execution_event(
        state,
        DispatchAuthorized(
            **new_envelope(session_id="s1", dedupe_key="auth"),
            role=ExecutionRole.EXIT,
            order_id="resting-tp",
            prepared_order_digest="digest",
        ),
    )
    reduce_execution_event(
        state,
        SubmissionAttempted(
            **new_envelope(session_id="s1", dedupe_key="attempt"),
            role=ExecutionRole.EXIT,
            order_id="resting-tp",
            attempt_id="a1",
        ),
    )
    reduce_execution_event(
        state,
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="resp"),
            role=ExecutionRole.EXIT,
            order_id="resting-tp",
            attempt_id="a1",
            accepted=True,
            venue_order_id="gtc-1",
            status="live",
            cumulative_matched_shares=Decimal("0"),
            trade_ids=(),
        ),
    )
    assert state.phase is ExecutionPhase.POSITION_OPEN
