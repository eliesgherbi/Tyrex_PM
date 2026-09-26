"""Pure reducer for the unified execution-session aggregate."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.execution.evidence import (
    BalanceObserved,
    CancelObserved,
    CancelRequested,
    DispatchAuthorized,
    DustResidualRecorded,
    ExecutionEvidence,
    ExecutionRole,
    ExitRequested,
    ExternalPositionAdjustmentObserved,
    ManualInterventionRequired,
    MarketSettlementObserved,
    OrderAttemptResolved,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    OrderSnapshotObserved,
    ReconciliationObserved,
    RecoveryActionAuthorized,
    RecoveryActionFinished,
    SessionOpened,
    SessionOperationalClearanceRecorded,
    SessionOperationalClearanceRevoked,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
    TradeAttributionCorrected,
    TradeStatus,
    TradeStatusObserved,
)
from tyrex_pm.execution.orders import OrderSide
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    KnownDustResidual,
    OperationalDisposition,
    OrderExecutionState,
    RecoveryAuthorization,
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


def _copy_prepare_timings(order: OrderExecutionState, event: Any) -> None:
    order.prepare_queue_wait_ms = getattr(event, "prepare_queue_wait_ms", None)
    order.tick_resolve_ms = getattr(event, "tick_resolve_ms", None)
    order.create_market_order_ms = getattr(event, "create_market_order_ms", None)
    order.refresh_wait_ms = getattr(event, "refresh_wait_ms", None)
    order.refresh_elapsed_ms = getattr(event, "refresh_elapsed_ms", None)
    order.order_metadata_cache_miss = bool(getattr(event, "order_metadata_cache_miss", False))


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


def _unapply_confirmed_trade(state: ExecutionSessionState, trade: TradeRecord) -> None:
    if not trade.position_applied:
        return
    state.confirmed_position_shares -= _confirmed_trade_delta(state, trade)
    trade.position_applied = False


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


def assert_clearance_appendable(state: ExecutionSessionState, event: object) -> None:
    """Reject a clearance that would poison replay. Does not mutate state."""
    proof = tuple(getattr(event, "proof_event_ids", ()) or ())
    disposition_name = str(getattr(event, "disposition", ""))
    try:
        disposition = OperationalDisposition(disposition_name)
    except ValueError as exc:
        raise ReducerInvariantError("unknown operational disposition") from exc
    if disposition is OperationalDisposition.UNRESOLVED:
        raise ReducerInvariantError("clearance cannot record unresolved")
    if not proof:
        raise ReducerInvariantError("clearance missing proof events")
    missing = [event_id for event_id in proof if event_id not in state.applied_event_ids]
    if missing:
        raise ReducerInvariantError("clearance missing proof events")
    if not _clearance_allowed(state, disposition):
        raise ReducerInvariantError("clearance prerequisites failed")


def _clearance_allowed(
    state: ExecutionSessionState, disposition: OperationalDisposition
) -> bool:
    if state.has_unresolved_mutation_attempt or state.entry_match_awaiting_confirmation:
        return False
    if state.resting_limit_venue_ids:
        return False
    if disposition is OperationalDisposition.SETTLEMENT_ONLY:
        return state.confirmed_position_shares <= 0 and state.settlement_holding_shares >= 0
    return state.confirmed_position_shares <= 0


def _invalidate_clearance_if_contradicted(state: ExecutionSessionState) -> None:
    disposition = state.operational_disposition
    if disposition is OperationalDisposition.UNRESOLVED:
        return
    contradicted = state.confirmed_position_shares > 0 or bool(state.resting_limit_venue_ids)
    if disposition is not OperationalDisposition.SETTLEMENT_ONLY:
        contradicted = contradicted or state.has_unresolved_mutation_attempt
    if contradicted:
        state.operational_disposition = OperationalDisposition.UNRESOLVED
        state.operational_proof_event_ids = ()


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
    # operator intervention or a classified leftover (known dust).
    locked_manual = state.phase is ExecutionPhase.MANUAL_INTERVENTION
    locked_dust = state.phase is ExecutionPhase.COMPLETED_FLAT_WITH_DUST

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
            state.opened_at = event.observed_at
            if state.baseline_position_shares < 0 or state.baseline_sellable_shares < 0:
                raise ReducerInvariantError("session baseline quantities must be non-negative")
            state.phase = ExecutionPhase.FLAT
    else:
        _require_session(state, event.session_id)

    if isinstance(event, OrderRequested):
        recovery = bool(getattr(event, "recovery", False))
        if event.role is ExecutionRole.ENTRY:
            if recovery:
                raise ReducerInvariantError("recovery cannot request a BUY")
            if state.terminal:
                raise ReducerInvariantError("terminal session cannot request an order")
        else:
            recovery_exit = (
                recovery
                and state.phase is ExecutionPhase.MANUAL_INTERVENTION
                and state.confirmed_position_shares > 0
            )
            if recovery and not recovery_exit:
                raise ReducerInvariantError(
                    "recovery EXIT is only valid for MANUAL sessions with confirmed exposure"
                )
            if state.terminal and not recovery_exit:
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
        _copy_prepare_timings(order, event)

    elif isinstance(event, OrderPreDispatchFailed):
        order = _order_for_event(state, event.role, event.order_id)
        if order.attempt_ids:
            raise ReducerInvariantError("pre-dispatch failure recorded after mutation attempt")
        order.pre_dispatch_stage = event.stage.value
        order.pre_dispatch_error_code = event.error_code
        order.requested_protection_price = event.requested_protection_price
        order.effective_protection_price = event.effective_protection_price
        order.tick_size = event.tick_size
        _copy_prepare_timings(order, event)
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
        elif event.accepted and str(event.status or "").lower() in {
            "live",
            "open",
            "resting",
        }:
            # Working GTC/GTD: maker bid waits for fill; resting TP keeps exposure.
            state.phase = (
                ExecutionPhase.POSITION_PENDING_CONFIRMATION
                if event.role is ExecutionRole.ENTRY
                else ExecutionPhase.POSITION_OPEN
            )
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
                elif locked_dust:
                    state.phase = ExecutionPhase.COMPLETED_FLAT_WITH_DUST
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
                elif locked_dust:
                    state.phase = ExecutionPhase.COMPLETED_FLAT_WITH_DUST
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

    elif isinstance(event, TradeAttributionCorrected):
        existing = state.trades.get(event.venue_trade_id)
        if existing is None:
            pass
        elif existing.attribution_digest == event.correction_digest:
            pass
        elif (
            existing.venue_order_id != event.previous_venue_order_id
            or existing.side != event.previous_side.value
            or existing.shares != event.previous_shares
            or existing.price != event.previous_price
            or existing.token_id != event.previous_token_id
        ):
            state.phase = ExecutionPhase.MANUAL_INTERVENTION
            state.last_error = "correction_previous_mismatch"
            effects.append(
                ReducerEffect(EffectKind.SESSION_TERMINAL, "correction_previous_mismatch")
            )
        elif (
            existing.venue_order_id == event.venue_order_id
            and existing.side == event.side.value
            and existing.shares == event.shares
            and existing.price == event.price
            and existing.token_id == event.token_id
        ):
            existing.attribution_digest = event.correction_digest
        elif (
            existing.attribution_digest
            and existing.attribution_digest != event.correction_digest
        ):
            state.phase = ExecutionPhase.MANUAL_INTERVENTION
            state.last_error = "conflicting_trade_identity"
            effects.append(
                ReducerEffect(EffectKind.SESSION_TERMINAL, "conflicting_trade_identity")
            )
        else:
            _unapply_confirmed_trade(state, existing)
            existing.venue_order_id = event.venue_order_id
            existing.local_order_id = event.order_id or existing.local_order_id
            existing.token_id = event.token_id
            existing.side = event.side.value
            existing.shares = event.shares
            existing.price = event.price
            existing.status = event.status
            existing.sources.add(event.source)
            existing.attribution_digest = event.correction_digest
            order = _order_by_identity(
                state, order_id=event.order_id, venue_order_id=event.venue_order_id
            )
            if order is None:
                state.phase = ExecutionPhase.RECONCILING
                effects.append(
                    ReducerEffect(EffectKind.REQUEST_RECONCILIATION, "unowned_trade")
                )
            else:
                order.venue_order_id = event.venue_order_id
                _apply_confirmed_trade(state, existing, effects)
                if state.confirmed_position_shares < 0:
                    state.phase = ExecutionPhase.MANUAL_INTERVENTION
                    state.last_error = "unrepairable_trade_attribution"
                    effects.append(
                        ReducerEffect(
                            EffectKind.SESSION_TERMINAL, "unrepairable_trade_attribution"
                        )
                    )

    elif isinstance(event, BalanceObserved):
        if state.identity is not None and event.token_id != state.identity.token_id:
            raise ReducerInvariantError("balance evidence token does not match session")
        balance = Decimal(str(event.balance_shares))
        allowance = None if event.allowance_shares is None else Decimal(str(event.allowance_shares))
        if balance < 0 or (allowance is not None and allowance < 0):
            raise ReducerInvariantError("balance and allowance must be non-negative")
        state.sellable_shares = balance if allowance is None else min(balance, allowance)
        state.last_balance_source = event.source

    elif isinstance(event, CancelRequested):
        pass

    elif isinstance(event, CancelObserved):
        remaining = [
            order_id
            for order_id in state.open_venue_order_ids
            if order_id not in set(event.canceled)
        ]
        state.open_venue_order_ids = tuple(remaining)
        for order in state.orders.values():
            if order.venue_order_id in event.canceled:
                order.venue_status = "cancelled"
            elif order.venue_order_id in event.matched_instead:
                order.venue_status = "matched"

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
        if state.operational_disposition is OperationalDisposition.SETTLEMENT_ONLY:
            if position <= state.settlement_holding_shares:
                position = Decimal("0")
            else:
                position = position - state.settlement_holding_shares
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

    elif isinstance(event, OrderAttemptResolved):
        order = state.orders.get(event.order_id)
        if order is None or event.attempt_id not in order.attempt_ids:
            raise ReducerInvariantError("attempt resolution names an unknown attempt")
        if event.attempt_id in state.resolved_attempt_ids:
            pass
        else:
            matched = Decimal(str(event.cumulative_matched_shares))
            if matched < 0:
                raise ReducerInvariantError("resolved match quantity must be non-negative")
            order.cumulative_matched_hwm = max(order.cumulative_matched_hwm, matched)
            order.venue_status = event.terminal_status
            order.ambiguous = False
            if order.accepted is None:
                order.accepted = matched > 0 or event.terminal_status.lower() not in {
                    "rejected",
                    "cancelled",
                    "canceled",
                    "expired",
                }
            if event.venue_order_id:
                order.venue_order_id = order.venue_order_id or event.venue_order_id
            state.resolved_attempt_ids.add(event.attempt_id)

    elif isinstance(event, ExternalPositionAdjustmentObserved):
        if state.identity is not None and event.token_id != state.identity.token_id:
            raise ReducerInvariantError("external adjustment token does not match session")
        if event.venue_trade_id and event.venue_trade_id in state.trades:
            raise ReducerInvariantError("external adjustment duplicates an owned trade")
        if event.activity_id in state.external_activity_ids:
            pass
        else:
            state.external_activity_ids.add(event.activity_id)
            state.external_reduction_shares += event.shares
            state.confirmed_position_shares = max(
                Decimal("0"), state.confirmed_position_shares - event.shares
            )

    elif isinstance(event, MarketSettlementObserved):
        if not event.supported:
            raise ReducerInvariantError("unsupported settlement proof")
        if state.identity is not None and (
            event.token_id != state.identity.token_id
            or event.condition_id != state.identity.market_id
        ):
            raise ReducerInvariantError("settlement identity does not match session")
        key = f"{event.condition_id}:{event.token_id}:{event.proof_source}"
        if key not in state.settlement_keys:
            state.settlement_keys.add(key)
            state.settlement_holding_shares += event.holding_shares
            state.settlement_excluded_shares += event.holding_shares
            state.confirmed_position_shares = max(
                Decimal("0"), state.confirmed_position_shares - event.holding_shares
            )
        state.operational_disposition = OperationalDisposition.SETTLEMENT_ONLY
        state.operational_proof_event_ids = (event.event_id,)

    elif isinstance(event, SessionOperationalClearanceRecorded):
        try:
            disposition = OperationalDisposition(event.disposition)
        except ValueError as exc:
            raise ReducerInvariantError("unknown operational disposition") from exc
        if disposition is OperationalDisposition.UNRESOLVED:
            raise ReducerInvariantError("clearance cannot record unresolved")
        if not event.proof_event_ids:
            raise ReducerInvariantError("clearance missing proof events")
        missing = [
            event_id
            for event_id in event.proof_event_ids
            if event_id not in state.applied_event_ids
        ]
        if missing:
            raise ReducerInvariantError("clearance missing proof events")
        if not _clearance_allowed(state, disposition):
            raise ReducerInvariantError("clearance prerequisites failed")
        state.operational_disposition = disposition
        state.operational_proof_event_ids = tuple(event.proof_event_ids)

    elif isinstance(event, SessionOperationalClearanceRevoked):
        state.operational_disposition = OperationalDisposition.UNRESOLVED
        state.operational_proof_event_ids = ()
        state.last_error = event.reason

    elif isinstance(event, RecoveryActionAuthorized):
        state.recovery_authorization = RecoveryAuthorization(
            action_id=event.action_id,
            token_id=event.token_id,
            condition_id=event.condition_id,
            max_shares=event.max_shares,
            min_price=event.min_price,
            expires_at=event.expires_at,
            generation=event.generation,
            action=event.action,
            venue_order_ids=tuple(event.venue_order_ids),
        )

    elif isinstance(event, RecoveryActionFinished):
        if (
            state.recovery_authorization is not None
            and state.recovery_authorization.action_id == event.action_id
        ):
            state.recovery_authorization = None

    elif isinstance(event, ManualInterventionRequired):
        state.phase = ExecutionPhase.MANUAL_INTERVENTION
        state.last_error = event.reason
        effects.append(ReducerEffect(EffectKind.SESSION_TERMINAL, event.reason))

    elif isinstance(event, DustResidualRecorded):
        state.dust_residual = KnownDustResidual(
            token_id=event.token_id,
            shares=event.shares,
            market_id=event.market_id,
            window_id=event.window_id,
            classification=event.classification,
            last_bid=event.last_bid,
            min_order_size=event.min_order_size,
            sell_size_increment=event.sell_size_increment,
            notional=event.notional,
            reason=event.reason,
        )
        state.phase = ExecutionPhase.COMPLETED_FLAT_WITH_DUST
        effects.append(ReducerEffect(EffectKind.SESSION_TERMINAL, event.classification))

    if locked_manual:
        state.phase = ExecutionPhase.MANUAL_INTERVENTION
    elif locked_dust:
        state.phase = ExecutionPhase.COMPLETED_FLAT_WITH_DUST
    if not isinstance(
        event,
        (SessionOperationalClearanceRecorded, MarketSettlementObserved),
    ):
        _invalidate_clearance_if_contradicted(state)

    state.applied_event_ids.add(event.event_id)
    state.event_count += 1
    return state, tuple(effects)
