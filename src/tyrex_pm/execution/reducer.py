"""Pure reducer for the unified execution-session aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tyrex_pm.execution.evidence import (
    BalanceObserved,
    DispatchAuthorized,
    ExecutionEvidence,
    ExecutionRole,
    ExitRequested,
    ManualInterventionRequired,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    OrderSnapshotObserved,
    ReconciliationObserved,
    SessionOpened,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
    TradeStatus,
    TradeStatusObserved,
)
from tyrex_pm.execution.orders import OrderSide
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    OrderExecutionState,
    SessionIdentity,
    TradeRecord,
)


class ReducerInvariantError(RuntimeError):
    pass


class EffectKind(str, Enum):
    REQUEST_RECONCILIATION = "REQUEST_RECONCILIATION"
    POSITION_CHANGED = "POSITION_CHANGED"
    ENTRY_CONFIRMED = "ENTRY_CONFIRMED"
    EXIT_CONFIRMED = "EXIT_CONFIRMED"
    SESSION_TERMINAL = "SESSION_TERMINAL"


@dataclass(frozen=True)
class ReducerEffect:
    kind: EffectKind
    reason: str


_STATUS_RANK = {
    TradeStatus.MATCHED: 1,
    TradeStatus.RETRYING: 2,
    TradeStatus.MINED: 3,
    TradeStatus.CONFIRMED: 4,
    TradeStatus.FAILED: 4,
}


def _require_session(state: ExecutionSessionState, session_id: str) -> None:
    if state.identity is None:
        raise ReducerInvariantError("session is not opened")
    if state.identity.session_id != session_id:
        raise ReducerInvariantError("event belongs to another session")


def _order_for_event(
    state: ExecutionSessionState, role: ExecutionRole, order_id: str
) -> OrderExecutionState:
    order = state.orders.get(order_id)
    if order is None:
        raise ReducerInvariantError("order is not requested")
    if order.role is not role:
        raise ReducerInvariantError("event role does not match requested order")
    return order


def _order_by_identity(
    state: ExecutionSessionState,
    *,
    order_id: str | None,
    venue_order_id: str,
) -> OrderExecutionState | None:
    for order in state.orders.values():
        if order is None:
            continue
        if order_id is not None and order.spec.order_id == order_id:
            return order
        if order.venue_order_id is not None and order.venue_order_id == venue_order_id:
            return order
    return None


def _confirmed_trade_delta(state: ExecutionSessionState, trade: TradeRecord) -> Decimal:
    return trade.shares if trade.side == OrderSide.BUY.value else -trade.shares


def _apply_confirmed_trade(
    state: ExecutionSessionState,
    trade: TradeRecord,
    effects: list[ReducerEffect],
) -> None:
    if trade.status is not TradeStatus.CONFIRMED or trade.position_applied:
        return
    state.confirmed_position_shares += _confirmed_trade_delta(state, trade)
    trade.position_applied = True
    if state.confirmed_position_shares < 0:
        state.phase = ExecutionPhase.RECONCILING
        effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "negative_position"))
        return
    effects.append(ReducerEffect(EffectKind.POSITION_CHANGED, "confirmed_trade"))
    if trade.side == OrderSide.BUY.value:
        state.phase = ExecutionPhase.POSITION_OPEN
        effects.append(ReducerEffect(EffectKind.ENTRY_CONFIRMED, "buy_confirmed"))
    elif state.confirmed_position_shares == 0:
        state.phase = ExecutionPhase.RECONCILING
        effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "sell_flat_candidate"))
    else:
        state.phase = ExecutionPhase.POSITION_OPEN


def reduce_execution_event(
    state: ExecutionSessionState,
    event: ExecutionEvidence,
) -> tuple[ExecutionSessionState, tuple[ReducerEffect, ...]]:
    """Apply one event in place and return effects.

    A coordinator owns the state and calls this function serially.  In-place
    mutation avoids copying potentially large trade maps on the hot path while
    keeping every transition deterministic and replayable.
    """
    if event.event_id in state.applied_event_ids:
        return state, ()
    effects: list[ReducerEffect] = []
    # Late venue observations must not reopen a session that already required
    # operator intervention (e.g. residual dust after exit_retry_budget_exhausted).
    locked_manual = state.phase is ExecutionPhase.MANUAL_INTERVENTION

    if isinstance(event, SessionOpened):
        if state.identity is not None:
            expected = state.identity
            observed = SessionIdentity(
                session_id=event.session_id,
                strategy_id=event.strategy_id,
                market_id=event.market_id,
                window_id=event.window_id,
                token_id=event.token_id,
            )
            if expected != observed:
                raise ReducerInvariantError("session identity cannot change")
        else:
            state.identity = SessionIdentity(
                session_id=event.session_id,
                strategy_id=event.strategy_id,
                market_id=event.market_id,
                window_id=event.window_id,
                token_id=event.token_id,
            )
            state.baseline_position_shares = Decimal(str(event.baseline_position_shares))
            state.baseline_sellable_shares = Decimal(str(event.baseline_sellable_shares))
            state.baseline_open_order_ids = tuple(event.baseline_open_order_ids)
            if state.baseline_position_shares < 0 or state.baseline_sellable_shares < 0:
                raise ReducerInvariantError("session baseline quantities must be non-negative")
            state.phase = ExecutionPhase.FLAT
    else:
        _require_session(state, event.session_id)

    if isinstance(event, OrderRequested):
        if state.terminal:
            raise ReducerInvariantError("terminal session cannot request an order")
        if event.order.order_id in state.orders:
            raise ReducerInvariantError("local order_id already exists")
        if event.role is ExecutionRole.ENTRY:
            if state.entry_order_ids:
                raise ReducerInvariantError("entry lineage already exists")
            if state.confirmed_position_shares != 0:
                raise ReducerInvariantError("cannot enter a session with exposure")
            order = OrderExecutionState(role=event.role, spec=event.order)
            state.orders[event.order.order_id] = order
            state.entry_order_ids.append(event.order.order_id)
            state.phase = ExecutionPhase.ENTRY_PREPARING
        else:
            if state.confirmed_position_shares <= 0:
                raise ReducerInvariantError("cannot exit without confirmed exposure")
            order = OrderExecutionState(role=event.role, spec=event.order)
            state.orders[event.order.order_id] = order
            state.exit_order_ids.append(event.order.order_id)
            state.phase = ExecutionPhase.EXIT_PREPARING

    elif isinstance(event, OrderPrepared):
        order = _order_for_event(state, event.role, event.order_id)
        order.prepared_order_digest = event.prepared_order_digest
        order.requested_protection_price = event.requested_protection_price
        order.effective_protection_price = event.effective_protection_price
        order.tick_size = event.tick_size

    elif isinstance(event, OrderPreDispatchFailed):
        order = _order_for_event(state, event.role, event.order_id)
        if order.attempt_ids:
            raise ReducerInvariantError("pre-dispatch failure recorded after mutation attempt")
        order.pre_dispatch_stage = event.stage.value
        order.pre_dispatch_error_code = event.error_code
        order.requested_protection_price = event.requested_protection_price
        order.effective_protection_price = event.effective_protection_price
        order.tick_size = event.tick_size
        order.last_error = f"{event.error_code}:{event.message}"
        state.last_error = order.last_error
        if event.role is ExecutionRole.ENTRY:
            state.phase = ExecutionPhase.COMPLETED_NO_DISPATCH
            effects.append(ReducerEffect(EffectKind.SESSION_TERMINAL, "entry_pre_dispatch_failed"))
        else:
            # An EXIT that was not posted leaves the confirmed position open.
            state.phase = ExecutionPhase.POSITION_OPEN

    elif isinstance(event, DispatchAuthorized):
        order = _order_for_event(state, event.role, event.order_id)
        order.prepared_order_digest = event.prepared_order_digest
        order.dispatch_authorized = True

    elif isinstance(event, SubmissionAttempted):
        order = _order_for_event(state, event.role, event.order_id)
        if not order.dispatch_authorized:
            raise ReducerInvariantError("submission attempted without durable authorization")
        if event.attempt_id not in order.attempt_ids:
            order.attempt_ids.append(event.attempt_id)
        state.phase = (
            ExecutionPhase.ENTRY_DISPATCHING
            if event.role is ExecutionRole.ENTRY
            else ExecutionPhase.EXIT_DISPATCHING
        )

    elif isinstance(event, SubmissionResponseObserved):
        order = _order_for_event(state, event.role, event.order_id)
        if event.attempt_id not in order.attempt_ids:
            raise ReducerInvariantError("submission response has no recorded attempt")
        order.accepted = event.accepted
        order.venue_order_id = event.venue_order_id or order.venue_order_id
        order.venue_status = event.status
        if event.cumulative_matched_shares is not None:
            observed = Decimal(str(event.cumulative_matched_shares))
            if observed < 0:
                raise ReducerInvariantError("matched shares cannot be negative")
            order.cumulative_matched_hwm = max(order.cumulative_matched_hwm, observed)
        order.trade_ids_from_response.update(event.trade_ids)
        if not event.accepted:
            order.last_error = event.error_code or event.message or "ORDER_REJECTED"
            if event.role is ExecutionRole.ENTRY:
                state.phase = ExecutionPhase.RECONCILING
                effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "entry_rejected"))
            else:
                state.phase = ExecutionPhase.POSITION_OPEN
        elif order.cumulative_matched_hwm > 0 or event.trade_ids:
            state.phase = ExecutionPhase.POSITION_PENDING_CONFIRMATION
        if order.venue_order_id is not None:
            for trade in state.trades.values():
                if trade.venue_order_id == order.venue_order_id:
                    if trade.local_order_id is None:
                        trade.local_order_id = order.spec.order_id
                    _apply_confirmed_trade(state, trade, effects)

    elif isinstance(event, SubmissionFailed):
        order = _order_for_event(state, event.role, event.order_id)
        if event.attempt_id not in order.attempt_ids:
            raise ReducerInvariantError("submission failure has no recorded attempt")
        order.ambiguous = event.ambiguous
        order.last_error = f"{event.error_class}:{event.message}"
        state.last_error = order.last_error
        if event.ambiguous:
            state.phase = ExecutionPhase.RECONCILING
            effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "ambiguous_submission"))
        elif event.role is ExecutionRole.ENTRY:
            state.phase = ExecutionPhase.RECONCILING
            effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "entry_submit_failed"))
        else:
            state.phase = ExecutionPhase.POSITION_OPEN

    elif isinstance(event, OrderSnapshotObserved):
        order = _order_by_identity(
            state, order_id=event.order_id, venue_order_id=event.venue_order_id
        )
        if order is None:
            state.phase = ExecutionPhase.RECONCILING
            effects.append(
                ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "unowned_order_evidence")
            )
        else:
            order.venue_order_id = event.venue_order_id
            order.venue_status = event.status
            observed = Decimal(str(event.cumulative_matched_shares))
            if observed < order.cumulative_matched_hwm:
                state.phase = ExecutionPhase.RECONCILING
                effects.append(
                    ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "matched_hwm_decreased")
                )
            else:
                order.cumulative_matched_hwm = observed

    elif isinstance(event, TradeStatusObserved):
        existing = state.trades.get(event.venue_trade_id)
        if existing is None:
            existing = TradeRecord(
                venue_trade_id=event.venue_trade_id,
                venue_order_id=event.venue_order_id,
                local_order_id=event.order_id,
                token_id=event.token_id,
                side=event.side.value,
                shares=event.shares,
                price=event.price,
                status=event.status,
                sources={event.source},
            )
            state.trades[event.venue_trade_id] = existing
        else:
            if (
                existing.venue_order_id != event.venue_order_id
                or existing.token_id != event.token_id
                or existing.side != event.side.value
                or existing.shares != event.shares
                or existing.price != event.price
            ):
                state.phase = ExecutionPhase.RECONCILING
                effects.append(
                    ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "conflicting_trade_identity")
                )
                if locked_manual:
                    state.phase = ExecutionPhase.MANUAL_INTERVENTION
                state.applied_event_ids.add(event.event_id)
                state.event_count += 1
                return state, tuple(effects)
            if {
                existing.status,
                event.status,
            } == {TradeStatus.CONFIRMED, TradeStatus.FAILED}:
                state.phase = ExecutionPhase.RECONCILING
                effects.append(
                    ReducerEffect(
                        EffectKind.REQUEST_RECONCILIATION,
                        "conflicting_trade_terminal_status",
                    )
                )
                existing.sources.add(event.source)
                if locked_manual:
                    state.phase = ExecutionPhase.MANUAL_INTERVENTION
                state.applied_event_ids.add(event.event_id)
                state.event_count += 1
                return state, tuple(effects)
            if _STATUS_RANK[event.status] >= _STATUS_RANK[existing.status]:
                existing.status = event.status
            existing.sources.add(event.source)

        order = _order_by_identity(
            state, order_id=event.order_id, venue_order_id=event.venue_order_id
        )
        if order is None:
            state.phase = ExecutionPhase.RECONCILING
            effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "unowned_trade"))
        else:
            order.venue_order_id = event.venue_order_id
            # A trade delta is never added to a cumulative HWM.  The HWM and
            # trade ledger are independent observations reconciled later.
            _apply_confirmed_trade(state, existing, effects)

    elif isinstance(event, BalanceObserved):
        if state.identity is not None and event.token_id != state.identity.token_id:
            raise ReducerInvariantError("balance evidence token does not match session")
        balance = Decimal(str(event.balance_shares))
        allowance = None if event.allowance_shares is None else Decimal(str(event.allowance_shares))
        if balance < 0 or (allowance is not None and allowance < 0):
            raise ReducerInvariantError("balance and allowance must be non-negative")
        state.sellable_shares = balance if allowance is None else min(balance, allowance)
        state.last_balance_source = event.source

    elif isinstance(event, ExitRequested):
        if state.confirmed_position_shares <= 0:
            raise ReducerInvariantError("exit requested without confirmed position")
        state.exit_requested_reason = event.reason
        state.protective_exit = event.protective
        if state.exit is None:
            state.phase = ExecutionPhase.EXIT_PREPARING

    elif isinstance(event, ReconciliationObserved):
        position = Decimal(str(event.confirmed_position_shares))
        sellable = Decimal(str(event.sellable_shares))
        if position < 0 or sellable < 0:
            raise ReducerInvariantError("reconciled quantities must be non-negative")
        state.confirmed_position_shares = position
        state.sellable_shares = sellable
        state.reconciliation_complete = event.complete
        state.reconciliation_notes = tuple(event.notes)
        state.open_venue_order_ids = tuple(event.open_order_ids)
        if not event.complete:
            state.phase = ExecutionPhase.RECONCILING
        elif position > 0:
            state.phase = ExecutionPhase.POSITION_OPEN
        elif event.open_order_ids:
            state.phase = ExecutionPhase.RECONCILING
        elif state.has_unresolved_mutation_attempt:
            state.phase = ExecutionPhase.RECONCILING
            effects.append(
                ReducerEffect(
                    EffectKind.REQUEST_RECONCILIATION,
                    "unresolved_mutation_attempt",
                )
            )
        elif state.entry_match_awaiting_confirmation:
            state.phase = ExecutionPhase.RECONCILING
            effects.append(
                ReducerEffect(
                    EffectKind.REQUEST_RECONCILIATION,
                    "entry_match_awaiting_confirmation",
                )
            )
        elif state.entry_authoritatively_unfilled:
            state.phase = ExecutionPhase.COMPLETED_NO_FILL
            effects.append(ReducerEffect(EffectKind.SESSION_TERMINAL, "no_fill_confirmed"))
        elif state.has_confirmed_buy and not state.has_confirmed_sell:
            # A balance snapshot can lag a confirmed BUY.  Absence of current
            # balance is not proof that the position was flattened.
            state.phase = ExecutionPhase.RECONCILING
            effects.append(ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "balance_lags_entry"))
        else:
            state.phase = ExecutionPhase.COMPLETED_FLAT
            effects.append(ReducerEffect(EffectKind.EXIT_CONFIRMED, "flat_confirmed"))
            effects.append(ReducerEffect(EffectKind.SESSION_TERMINAL, "flat_confirmed"))

    elif isinstance(event, ManualInterventionRequired):
        state.phase = ExecutionPhase.MANUAL_INTERVENTION
        state.last_error = event.reason
        effects.append(ReducerEffect(EffectKind.SESSION_TERMINAL, event.reason))

    if locked_manual:
        state.phase = ExecutionPhase.MANUAL_INTERVENTION

    state.applied_event_ids.add(event.event_id)
    state.event_count += 1
    return state, tuple(effects)
