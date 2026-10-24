"""R1 recovery-only flatten/cancel from restored MANUAL sessions."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from support.runtime import bare_runtime

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, FlattenIntent, new_intent_id
from tyrex_pm.execution.account_state import AccountStateAuthority, AccountStatePolicy
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    DispatchBlocked,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import (
    ExecutionRole,
    ManualInterventionRequired,
    OrderRequested,
    ReconciliationObserved,
    new_envelope,
)
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.orders import LimitOrderSpec, MarketBuyOrderSpec, OrderSide, TimeInForce
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.reducer import ReducerInvariantError, reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.runtime.capabilities import CapabilityController

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=timezone.utc)


class _RecoveryVenue:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.canceled: list[tuple[str, ...]] = []
        self.cancel_all_called = False
        self.balance = Decimal("7")
        self.open_ids: tuple[str, ...] = ()
        self.trades: list[SimpleNamespace] = []
        self.late_fill = False

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def post_order(self, prepared):  # noqa: ANN001
        side = prepared.payload.side.value
        self.posts.append(side)
        if side == "BUY":
            raise AssertionError("recovery must never POST a BUY")
        shares = prepared.payload.shares
        self.balance = Decimal("0")
        self.open_ids = ()
        venue_id = "venue-exit"
        trade = SimpleNamespace(
            id=f"rec-{len(self.trades)+1}",
            taker_order_id=venue_id,
            maker_orders=(),
            status="CONFIRMED",
            side="SELL",
            size=shares,
            price=Decimal("0.40"),
            asset_id="token",
            updated_at=NOW,
        )
        self.trades.append(trade)
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="venue-exit",
            status="matched",
            cumulative_matched_shares=shares,
            trade_ids=(trade.id,),
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        ids = tuple(order_ids)
        self.canceled.append(ids)
        if self.late_fill:
            self.balance = Decimal("7")
            trade = SimpleNamespace(
                id="late-fill",
                taker_order_id=ids[0] if ids else "live-1",
                maker_orders=(),
                status="CONFIRMED",
                side="BUY",
                size=Decimal("7"),
                price=Decimal("0.40"),
                asset_id="token",
                updated_at=NOW,
            )
            self.trades.append(trade)
        self.open_ids = ()
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        return cancel_result_from_sdk({"canceled": ids, "not_canceled": {}})

    async def cancel_all(self) -> None:
        self.cancel_all_called = True

    async def list_open_orders(self, **_kwargs):
        return tuple({"id": item} for item in self.open_ids)

    async def list_account_trades(self, **_kwargs):
        return tuple(self.trades)

    async def conditional_balance(self, _token_id):
        return self.balance, Decimal("100")

    async def collateral_balance(self):
        return Decimal("20"), Decimal("20")

    async def close(self):
        return None


def _lifecycle(venue: _RecoveryVenue) -> ExecutionLifecycle:
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
            exit_retry_limit=3,
            exit_retry_budget_s=1,
            cancel_delay_s=0.001,
        ),
    )


def _flatten() -> FlattenIntent:
    return FlattenIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("z_gap"),
        instrument_id=InstrumentId("token"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("recovery"),
        causation_id=None,
        reason_code="RECOVERY_FLATTEN",
    )


def _entry() -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("z_gap"),
        instrument_id=InstrumentId("token"),
        market_id=MarketId("market"),
        created_at=NOW,
        correlation_id=CorrelationId("recovery"),
        causation_id=None,
        reason_code="ENTRY_CANDIDATE",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.51"),
    )


async def _open_manual_with_exposure(lifecycle: ExecutionLifecycle) -> None:
    await lifecycle.open(
        session_id="session",
        strategy_id="z_gap",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    await lifecycle.coordinator.apply(
        ReconciliationObserved(
            **new_envelope(session_id="session", dedupe_key="recon-pos"),
            source="test",
            complete=True,
            confirmed_position_shares=Decimal("7"),
            sellable_shares=Decimal("7"),
            open_order_ids=(),
        )
    )
    await lifecycle.coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="session", dedupe_key="manual:restored"),
            reason="restored_manual",
        )
    )


@pytest.mark.asyncio
async def test_restored_manual_with_exposure_posts_recovery_sell() -> None:
    venue = _RecoveryVenue()
    lifecycle = _lifecycle(venue)
    await _open_manual_with_exposure(lifecycle)
    assert lifecycle.state.phase is ExecutionPhase.MANUAL_INTERVENTION
    state = await lifecycle.drain_open_session(
        "shutdown", min_order_size=Decimal("0.01"), last_bid=Decimal("0.40")
    )
    assert "SELL" in venue.posts
    assert "BUY" not in venue.posts
    assert state.phase is ExecutionPhase.MANUAL_INTERVENTION
    assert state.confirmed_position_shares == Decimal("0")
    assert not state.safe_to_open_entry
    journal = [type(event).__name__ for event in lifecycle.coordinator.journal.load("session")]
    assert "ManualInterventionRequired" in journal
    assert journal.count("ManualInterventionRequired") >= 1
    venue.collateral = Decimal("20")

    async def collateral_balance():
        return Decimal("20"), Decimal("20")

    venue.collateral_balance = collateral_balance  # type: ignore[method-assign]
    snapshot = await AccountStateAuthority(
        venue, policy=AccountStatePolicy()
    ).observe(market_id="market", token_ids=("token",), required_collateral=Decimal("5"))
    assert snapshot.tokens[0].balance_shares == Decimal("0")
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_restored_manual_live_order_cancels_then_flattens_late_fill() -> None:
    venue = _RecoveryVenue()
    venue.open_ids = ("live-1",)
    venue.late_fill = True
    lifecycle = _lifecycle(venue)
    await lifecycle.open(
        session_id="session",
        strategy_id="z_gap",
        market_id="market",
        window_id="window",
        token_id="token",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    spec = LimitOrderSpec(
        order_id="entry-1",
        market_id="market",
        instrument_id="token",
        token_id="token",
        side=OrderSide.BUY,
        limit_price=Decimal("0.40"),
        shares=Decimal("7"),
        time_in_force=TimeInForce.GTC,
    )
    from tyrex_pm.execution.evidence import (
        DispatchAuthorized,
        SubmissionAttempted,
        SubmissionResponseObserved,
    )

    await lifecycle.coordinator.apply(
        OrderRequested(
            **new_envelope(session_id="session", dedupe_key="req"),
            role=ExecutionRole.ENTRY,
            order=spec,
        )
    )
    await lifecycle.coordinator.apply(
        DispatchAuthorized(
            **new_envelope(session_id="session", dedupe_key="auth"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            prepared_order_digest="d1",
        )
    )
    await lifecycle.coordinator.apply(
        SubmissionAttempted(
            **new_envelope(session_id="session", dedupe_key="att"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
        )
    )
    await lifecycle.coordinator.apply(
        SubmissionResponseObserved(
            **new_envelope(session_id="session", dedupe_key="resp"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="a1",
            accepted=True,
            venue_order_id="live-1",
            status="live",
        )
    )
    await lifecycle.coordinator.apply(
        ManualInterventionRequired(
            **new_envelope(session_id="session", dedupe_key="manual:restored"),
            reason="restored_manual",
        )
    )
    state = await lifecycle.drain_open_session(
        "shutdown", min_order_size=Decimal("0.01"), last_bid=Decimal("0.40")
    )
    assert venue.canceled
    assert not venue.cancel_all_called
    assert "SELL" in venue.posts
    assert "BUY" not in venue.posts
    assert state.phase is ExecutionPhase.MANUAL_INTERVENTION
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_recovery_failure_remains_blocked() -> None:
    venue = _RecoveryVenue()

    async def fail_post(prepared):  # noqa: ANN001
        venue.posts.append("FAILED")
        raise TimeoutError("venue timeout")

    venue.post_order = fail_post  # type: ignore[method-assign]
    lifecycle = _lifecycle(venue)
    lifecycle.policy = LifecyclePolicy(
        evidence_timeout_s=0.05,
        reconciliation_interval_s=0.001,
        exit_retry_limit=1,
        exit_retry_budget_s=0.05,
        cancel_delay_s=0.001,
    )
    await _open_manual_with_exposure(lifecycle)
    state = await lifecycle.drain_open_session(
        "shutdown", min_order_size=Decimal("0.01"), last_bid=Decimal("0.40")
    )
    assert state.phase is ExecutionPhase.MANUAL_INTERVENTION
    assert not state.safe_to_open_entry
    assert "BUY" not in venue.posts
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_incomplete_scoped_evidence_blocks(monkeypatch) -> None:
    runtime = bare_runtime()
    runtime.known_dust_residuals = []
    runtime.capabilities = CapabilityController()
    records: list[tuple[str, dict]] = []
    runtime.run_recorder = type(
        "Rec", (), {"record": staticmethod(lambda kind, payload: records.append((kind, payload)))}
    )()
    runtime.config = type(
        "Cfg", (), {"risk": type("Risk", (), {"maximum_total_debit": Decimal("5")})()}
    )()

    class _Incomplete:
        async def collateral_balance(self):
            raise TimeoutError("unavailable")

        async def conditional_balance(self, _token_id):
            raise TimeoutError("unavailable")

        async def list_open_orders(self, **_kwargs):
            raise TimeoutError("unavailable")

    runtime.account_state_authority = AccountStateAuthority(
        _Incomplete(), policy=AccountStatePolicy(read_timeout_s=0.01, retry_attempts=1)
    )
    restored = ExecutionSessionState()
    restored, _ = reduce_execution_event(
        restored,
        __import__("tyrex_pm.execution.evidence", fromlist=["SessionOpened"]).SessionOpened(
            **new_envelope(session_id="old", dedupe_key="open"),
            strategy_id="z_gap",
            market_id="old-market",
            window_id="old-w",
            token_id="yes-old",
        ),
    )
    restored, _ = reduce_execution_event(
        restored,
        ManualInterventionRequired(
            **new_envelope(session_id="old", dedupe_key="man"),
            reason="leftover",
        ),
    )
    action = await runtime._inspect_retained_session(restored, bound_market_id="new-market")
    assert action == "block_foreign"


@pytest.mark.asyncio
async def test_recovery_cannot_submit_second_buy() -> None:
    venue = _RecoveryVenue()
    lifecycle = _lifecycle(venue)
    await _open_manual_with_exposure(lifecycle)
    with pytest.raises(RuntimeError, match="recovery cannot request a BUY"):
        await lifecycle.submit_entry(_entry(), token_id="token")
    with pytest.raises(DispatchBlocked, match="recovery cannot request a BUY"):
        await lifecycle.coordinator.submit(
            session_id="session",
            role=ExecutionRole.ENTRY,
            spec=MarketBuyOrderSpec(
                order_id="evil-buy",
                market_id="market",
                instrument_id="token",
                token_id="token",
                spend_amount=Decimal("5"),
                maximum_total_debit=Decimal("5"),
                worst_price=Decimal("0.5"),
            ),
            recovery=True,
        )
    state = ExecutionSessionState()
    state.phase = ExecutionPhase.MANUAL_INTERVENTION
    state.confirmed_position_shares = Decimal("7")
    from tyrex_pm.execution.session_state import SessionIdentity

    state.identity = SessionIdentity(
        session_id="s1",
        strategy_id="z_gap",
        market_id="m",
        window_id="w",
        token_id="t",
    )
    with pytest.raises(ReducerInvariantError, match="recovery cannot request a BUY"):
        reduce_execution_event(
            state,
            OrderRequested(
                **new_envelope(session_id="s1", dedupe_key="buy"),
                role=ExecutionRole.ENTRY,
                order=MarketBuyOrderSpec(
                    order_id="evil-buy-2",
                    market_id="m",
                    instrument_id="t",
                    token_id="t",
                    spend_amount=Decimal("5"),
                    maximum_total_debit=Decimal("5"),
                    worst_price=Decimal("0.5"),
                ),
                recovery=True,
            ),
        )
    empty = ExecutionSessionState()
    empty.phase = ExecutionPhase.MANUAL_INTERVENTION
    empty.confirmed_position_shares = Decimal("0")
    empty.identity = state.identity
    from tyrex_pm.execution.orders import MarketSellOrderSpec

    with pytest.raises(ReducerInvariantError, match="confirmed exposure"):
        reduce_execution_event(
            empty,
            OrderRequested(
                **new_envelope(session_id="s1", dedupe_key="exit-empty"),
                role=ExecutionRole.EXIT,
                order=MarketSellOrderSpec(
                    order_id="evil-exit",
                    market_id="m",
                    instrument_id="t",
                    token_id="t",
                    shares=Decimal("1"),
                    minimum_price=Decimal("0.01"),
                ),
                recovery=True,
            ),
        )
    await lifecycle.coordinator.close()
