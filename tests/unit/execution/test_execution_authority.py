from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    OrderPreparationFailed,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    ExecutionRole,
    OrderPreDispatchFailed,
    ReconciliationObserved,
    SessionOpened,
    SubmissionAttempted,
    SubmissionResponseObserved,
    TradeStatus,
    TradeStatusObserved,
    new_envelope,
)
from tyrex_pm.execution.orders import MarketBuyOrderSpec, MarketSellOrderSpec, OrderSide
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.reducer import reduce_execution_event
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal, SqliteExecutionJournal

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def opened(session_id: str = "s1") -> SessionOpened:
    return SessionOpened(
        **new_envelope(session_id=session_id, dedupe_key="session-opened"),
        strategy_id="z_gap",
        market_id="market-1",
        window_id="btc-5m-1",
        token_id="yes-token",
    )


def buy(order_id: str = "entry-1") -> MarketBuyOrderSpec:
    return MarketBuyOrderSpec(
        order_id=order_id,
        market_id="market-1",
        instrument_id="market-1:YES",
        token_id="yes-token",
        spend_amount=Decimal("4.83"),
        maximum_total_debit=Decimal("5"),
        worst_price=Decimal("0.51"),
        estimated_shares=Decimal("9.470588"),
    )


def trade(*, event_id: str, status: TradeStatus, shares: str = "9.857141") -> TradeStatusObserved:
    return TradeStatusObserved(
        session_id="s1",
        event_id=event_id,
        observed_at=NOW,
        dedupe_key=f"trade:t1:{status.value}",
        source="user_stream",
        venue_trade_id="t1",
        venue_order_id="venue-entry",
        order_id="entry-1",
        token_id="yes-token",
        side=OrderSide.BUY,
        shares=Decimal(shares),
        price=Decimal("0.49"),
        status=status,
    )


def test_market_buy_estimated_shares_are_not_a_fill_cap() -> None:
    spec = buy()
    assert spec.estimated_shares == Decimal("9.470588")
    assert Decimal("9.857141") > spec.estimated_shares


def test_trade_status_progression_confirms_quantity_once() -> None:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(state, opened())
    from tyrex_pm.execution.evidence import OrderRequested

    state, _ = reduce_execution_event(
        state,
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="entry-request"),
            role=ExecutionRole.ENTRY,
            order=buy(),
        ),
    )
    for event in (
        trade(event_id="matched", status=TradeStatus.MATCHED),
        trade(event_id="mined", status=TradeStatus.MINED),
        trade(event_id="confirmed", status=TradeStatus.CONFIRMED),
        trade(event_id="confirmed-replay", status=TradeStatus.CONFIRMED),
    ):
        state, _ = reduce_execution_event(state, event)
    assert state.confirmed_position_shares == Decimal("9.857141")
    assert state.phase is ExecutionPhase.POSITION_OPEN
    assert len(state.trades) == 1


def test_conflicting_trade_identity_requires_reconciliation() -> None:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(state, opened())
    from tyrex_pm.execution.evidence import OrderRequested

    state, _ = reduce_execution_event(
        state,
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="entry-request"),
            role=ExecutionRole.ENTRY,
            order=buy(),
        ),
    )
    state, _ = reduce_execution_event(state, trade(event_id="matched", status=TradeStatus.MATCHED))
    state, effects = reduce_execution_event(
        state,
        trade(event_id="conflict", status=TradeStatus.CONFIRMED, shares="10"),
    )
    assert state.phase is ExecutionPhase.RECONCILING
    assert any(effect.reason == "conflicting_trade_identity" for effect in effects)


def test_balance_zero_cannot_erase_a_confirmed_buy() -> None:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(state, opened())
    from tyrex_pm.execution.evidence import OrderRequested

    state, _ = reduce_execution_event(
        state,
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="entry-request"),
            role=ExecutionRole.ENTRY,
            order=buy(),
        ),
    )
    state, _ = reduce_execution_event(
        state, trade(event_id="confirmed", status=TradeStatus.CONFIRMED)
    )
    state, _ = reduce_execution_event(
        state,
        ReconciliationObserved(
            **new_envelope(session_id="s1", dedupe_key="recon-flat"),
            source="authenticated_rest",
            complete=True,
            confirmed_position_shares=Decimal("0"),
            sellable_shares=Decimal("0"),
            open_order_ids=(),
        ),
    )
    assert state.phase is ExecutionPhase.RECONCILING


@pytest.mark.parametrize(
    ("accepted", "matched", "trade_ids", "expected"),
    [
        (True, Decimal("9"), ("expected-trade",), ExecutionPhase.RECONCILING),
        (False, Decimal("0"), (), ExecutionPhase.COMPLETED_NO_FILL),
    ],
)
def test_no_fill_requires_authoritative_unfilled_response(
    accepted: bool,
    matched: Decimal,
    trade_ids: tuple[str, ...],
    expected: ExecutionPhase,
) -> None:
    state = ExecutionSessionState()
    state, _ = reduce_execution_event(state, opened())
    from tyrex_pm.execution.evidence import OrderRequested

    events = (
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="entry-request"),
            role=ExecutionRole.ENTRY,
            order=buy(),
        ),
        DispatchAuthorized(
            **new_envelope(session_id="s1", dedupe_key="authorized"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            prepared_order_digest="digest",
        ),
        SubmissionAttempted(
            **new_envelope(session_id="s1", dedupe_key="attempt"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="attempt-1",
        ),
        SubmissionResponseObserved(
            **new_envelope(session_id="s1", dedupe_key="response"),
            role=ExecutionRole.ENTRY,
            order_id="entry-1",
            attempt_id="attempt-1",
            accepted=accepted,
            venue_order_id="venue-entry" if accepted else None,
            status="matched" if accepted else "rejected",
            cumulative_matched_shares=matched,
            trade_ids=trade_ids,
            error_code=None if accepted else "fak_not_filled",
        ),
        ReconciliationObserved(
            **new_envelope(session_id="s1", dedupe_key="reconcile"),
            source="readonly_rest",
            complete=True,
            confirmed_position_shares=Decimal("0"),
            sellable_shares=Decimal("0"),
            open_order_ids=(),
        ),
    )
    for event in events:
        state, _ = reduce_execution_event(state, event)
    assert state.phase is expected


class _LaggingBalanceGateway:
    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **_kwargs):
        return ()

    async def conditional_balance(self, _token_id):
        return Decimal("0"), Decimal("100")


@pytest.mark.asyncio
async def test_reconciler_preserves_confirmed_trade_when_balance_lags() -> None:
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=_Gateway(),
        final_gate=lambda _: asyncio.sleep(0, result=(True, None)),
    )
    await coordinator.apply(opened())
    from tyrex_pm.execution.evidence import OrderRequested

    await coordinator.apply(
        OrderRequested(
            **new_envelope(session_id="s1", dedupe_key="entry-request"),
            role=ExecutionRole.ENTRY,
            order=buy(),
        )
    )
    await coordinator.apply(trade(event_id="confirmed", status=TradeStatus.CONFIRMED))
    result = await SessionReconciler(_LaggingBalanceGateway()).reconcile(coordinator, "s1")
    assert result.position_shares == Decimal("9.857141")
    assert coordinator.state("s1").phase is ExecutionPhase.POSITION_OPEN
    await coordinator.close()


def test_sqlite_journal_is_durable_and_semantically_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "execution.sqlite3"
    journal = SqliteExecutionJournal(path)
    event = opened()
    assert journal.append(event) is True
    assert journal.append(event) is False
    journal.close()

    reopened = SqliteExecutionJournal(path)
    assert reopened.load("s1") == (event,)
    assert reopened.mutation_attempt_count("s1") == 0
    reopened.close()


class _Gateway:
    def __init__(self) -> None:
        self.trace: list[str] = []

    async def prepare_order(self, spec):  # noqa: ANN001
        self.trace.append("prepare")
        return PreparedOrder(local_order_id=spec.order_id, digest="signed-digest", payload={})

    async def post_order(self, prepared):  # noqa: ANN001
        self.trace.append("post")
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="venue-entry",
            status="matched",
            cumulative_matched_shares=Decimal("9.857141"),
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        self.trace.append("discard")

    async def close(self) -> None:
        self.trace.append("close")


@pytest.mark.asyncio
async def test_coordinator_durably_records_attempt_before_post() -> None:
    gateway = _Gateway()
    journal = MemoryExecutionJournal()
    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=gateway,
        final_gate=lambda _: asyncio.sleep(0, result=(True, None)),
    )
    await coordinator.apply(opened())
    result = await coordinator.submit(session_id="s1", role=ExecutionRole.ENTRY, spec=buy())
    assert result.accepted
    assert gateway.trace[:2] == ["prepare", "post"]
    assert journal.mutation_attempt_count("s1") == 1
    assert coordinator.state("s1").entry is not None
    assert coordinator.state("s1").entry.cumulative_matched_hwm == Decimal("9.857141")
    await coordinator.close()


@pytest.mark.asyncio
async def test_preparation_failure_is_durable_terminal_and_never_a_mutation() -> None:
    class FailedPreparationGateway(_Gateway):
        async def prepare_order(self, spec):  # noqa: ANN001
            self.trace.append("prepare")
            error = RuntimeError("price is not on the venue tick grid")
            error.error_code = "PRICE_ADAPTATION_FAILED"  # type: ignore[attr-defined]
            error.preparation_details = {  # type: ignore[attr-defined]
                "requested_protection_price": Decimal("0.7146489189623736"),
                "effective_protection_price": None,
                "tick_size": Decimal("0.01"),
            }
            raise error

    gateway = FailedPreparationGateway()
    journal = MemoryExecutionJournal()
    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=gateway,
        final_gate=lambda _: asyncio.sleep(0, result=(True, None)),
    )
    await coordinator.apply(opened())
    with pytest.raises(OrderPreparationFailed, match="PRICE_ADAPTATION_FAILED"):
        await coordinator.submit(session_id="s1", role=ExecutionRole.ENTRY, spec=buy())

    state = coordinator.state("s1")
    assert state.phase is ExecutionPhase.COMPLETED_NO_DISPATCH
    assert state.mutations_attempted == 0
    assert gateway.trace == ["prepare"]
    failed = next(
        event for event in journal.load("s1") if isinstance(event, OrderPreDispatchFailed)
    )
    assert failed.error_code == "PRICE_ADAPTATION_FAILED"
    assert failed.tick_size == Decimal("0.01")
    await coordinator.close()


@pytest.mark.asyncio
async def test_cancelled_post_is_durably_ambiguous_before_cancellation_propagates() -> None:
    class CancelledPostGateway(_Gateway):
        async def post_order(self, prepared):  # noqa: ANN001
            self.trace.append("post")
            raise asyncio.CancelledError

    gateway = CancelledPostGateway()
    journal = MemoryExecutionJournal()
    coordinator = AccountExecutionCoordinator(
        journal=journal,
        gateway=gateway,
        final_gate=lambda _: asyncio.sleep(0, result=(True, None)),
    )
    await coordinator.apply(opened())
    with pytest.raises(asyncio.CancelledError):
        await coordinator.submit(session_id="s1", role=ExecutionRole.ENTRY, spec=buy())
    state = coordinator.state("s1")
    assert journal.mutation_attempt_count("s1") == 1
    assert state.entry is not None
    assert state.entry.ambiguous
    assert state.entry.last_error is not None
    assert "UNCERTAIN_DISPATCH" in state.entry.last_error
    await coordinator.close()


def test_sell_is_share_denominated() -> None:
    spec = MarketSellOrderSpec(
        order_id="exit-1",
        market_id="market-1",
        instrument_id="market-1:YES",
        token_id="yes-token",
        shares=Decimal("9.85"),
        minimum_price=Decimal("0.40"),
    )
    assert spec.shares == Decimal("9.85")
