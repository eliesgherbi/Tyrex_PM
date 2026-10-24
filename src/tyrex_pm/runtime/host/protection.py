"""Protective exits: stop loss, take profit (reactive and resting), dust."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.execution.coordinator import DispatchBlocked
from tyrex_pm.execution.evidence import (
    ExecutionRole,
)
from tyrex_pm.execution.exit_sizing import (
    can_post_exit,
    remaining_exit_shares,
    venue_legal_sell_shares,
)
from tyrex_pm.execution.orders import TimeInForce
from tyrex_pm.execution.session_state import (
    ExecutionSessionState,
)
from tyrex_pm.protection import (
    ArmedProtection,
    ProtectionPhase,
    build_protection_intent,
    mark_triggered,
    observe_mark,
)
from tyrex_pm.runtime.host.shared import (
    _hold_committed,
    ready_binding_strategy_id_from_session,
    ready_fallback_correlation,
)
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime, ZGapMarketRuntime


class ProtectionMixin:
    """Protective exits: stop loss, take profit (reactive and resting), dust.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def on_public_fact(self, fact: str, market_runtime: MarketSessionRuntime) -> None:
        del fact
        self._maybe_arm_protection(market_runtime)
        self._maybe_emit_protection_exit(market_runtime)

    def _held_best_bid(self, market_runtime: MarketSessionRuntime) -> Decimal | None:
        del market_runtime
        if self.selected_instrument is None or self.feed_supervisor is None:
            return None
        view = self.feed_supervisor.active_view()
        if view is None:
            return None
        token = self.selected_token_id
        for leg in (view.up, view.down):
            if leg.token_id == token:
                return leg.quote.best_bid
        return None

    def _held_min_order_size(self) -> Decimal | None:
        """BookView first, then binding/store. Missing min size is fail-closed."""
        token = self.selected_token_id
        if token is None or self.feed_supervisor is None:
            return None
        view = self.feed_supervisor.active_view()
        if view is not None:
            for leg in (view.up, view.down):
                if leg.token_id == token and leg.min_order_size is not None:
                    return leg.min_order_size
        feed = self.feed_supervisor.active
        if feed is None or not feed.binding.owns_token(token):
            return None
        if feed.binding.min_order_size is not None:
            return feed.binding.min_order_size
        return feed.store.get(InstrumentId(token)).min_order_size

    def _remember_dust(self, state: ExecutionSessionState | None) -> None:
        if state is None or state.dust_residual is None:
            return
        residual = state.dust_residual
        if any(
            existing.token_id == residual.token_id and existing.shares == residual.shares
            for existing in self.known_dust_residuals
        ):
            return
        self.known_dust_residuals.append(residual)

    def _maybe_arm_protection(self, market_runtime: ZGapMarketRuntime) -> None:
        if self._recovery_policy_unresolved:
            return
        spec = self.config.protection
        state = self.lifecycle.state
        if (
            spec is None
            or state is None
            or state.terminal
            or not state.has_exposure
            or state.identity is None
            or self.selected_instrument is None
            or self.active_market is None
        ):
            return
        existing = self.protection_registry.get(state.session_id)
        if existing is not None and existing.phase in {
            ProtectionPhase.ARMED,
            ProtectionPhase.TRIGGERED,
        }:
            return
        shares = state.confirmed_position_shares
        cost = self._position_cost(state)
        if shares <= 0 or cost <= 0:
            return
        entry_price = cost / shares
        mark = self._held_best_bid(market_runtime)
        initial_mark = mark if mark is not None else entry_price
        armed = ArmedProtection.arm(
            session_id=state.session_id,
            strategy_id=ready_binding_strategy_id_from_session(state),
            market_id=self.active_market.market.market_id,
            instrument_id=self.selected_instrument.instrument_id,
            spec=spec,
            entry_price=entry_price,
            confirmed_quantity=shares,
            armed_at=datetime.now(timezone.utc),
            initial_mark=initial_mark,
        )
        self.protection_registry.put(armed)
        self.run_recorder.record(
            "PROTECTION_ARMED",
            {
                "session_id": state.session_id,
                "entry_price": str(entry_price),
                "confirmed_quantity": str(shares),
                "thresholds": dict(armed.thresholds.evidence),
            },
        )

    def _maybe_emit_protection_exit(self, market_runtime: ZGapMarketRuntime) -> None:
        if _hold_committed(self):
            return
        state = self.lifecycle.state
        if state is None or not state.has_exposure:
            if state is not None:
                self.protection_registry.disarm(state.session_id)
            return
        armed = self.protection_registry.get(state.session_id)
        if armed is None or armed.phase is not ProtectionPhase.ARMED:
            return
        if state.exit_requested_reason:
            return
        mark = self._held_best_bid(market_runtime)
        if mark is None:
            return
        tp = armed.thresholds.take_profit
        style = None if armed.spec.take_profit is None else armed.spec.take_profit.style
        skip_tp = bool(armed.resting_venue_order_id) or (
            style in {"resting_gtc", "resting_gtd"}
            and not armed.resting_fallback_reason
            and tp is not None
            and mark < tp
        )
        decision = observe_mark(armed, mark=mark, skip_take_profit=skip_tp)
        if not decision.fired or decision.reason is None:
            return
        mark_triggered(armed, decision)
        intent = build_protection_intent(
            armed,
            reason=decision.reason,
            created_at=datetime.now(timezone.utc),
            correlation_id=ready_fallback_correlation(),
            causation_id=None,
            evidence=dict(decision.evidence),
        )
        key = intent.semantic_key()
        if key in self.seen_intents:
            return
        self.seen_intents.add(key)
        self._enqueue_intent(intent)
        self.run_recorder.record(
            "PROTECTION_TRIGGERED",
            {
                "session_id": state.session_id,
                "reason_code": decision.reason.value,
                "mark": str(mark),
                "evidence": dict(decision.evidence),
            },
        )

    async def _maybe_post_resting_take_profit(self, market_runtime: ZGapMarketRuntime) -> None:
        spec = self.config.protection
        state = self.lifecycle.state
        if (
            spec is None
            or spec.take_profit is None
            or spec.take_profit.style not in {"resting_gtc", "resting_gtd"}
            or state is None
            or state.terminal
            or not state.has_exposure
            or state.identity is None
            or self.selected_instrument is None
            or self.active_market is None
            or self.selected_token_id is None
        ):
            return
        armed = self.protection_registry.get(state.session_id)
        if armed is None or armed.phase is not ProtectionPhase.ARMED:
            return
        if armed.resting_venue_order_id:
            self._watch_resting_take_profit(armed, state)
            return
        if state.exit_requested_reason or armed.resting_fallback_reason:
            return
        mark = self._held_best_bid(market_runtime)
        tp = armed.thresholds.take_profit
        if mark is None or tp is None or mark >= tp:
            return
        expiration = None
        time_in_force = TimeInForce.GTC
        if spec.take_profit.style == "resting_gtd":
            event_end = self.active_market.market.event_end
            if event_end is None:
                armed.resting_fallback_reason = "gtd_event_end_unknown"
                return
            remain_s = (event_end - datetime.now(timezone.utc)).total_seconds()
            if remain_s < 180:
                armed.resting_fallback_reason = "gtd_expiration_below_3m"
                return
            time_in_force = TimeInForce.GTD
            expiration = int(event_end.timestamp())
        remaining = remaining_exit_shares(state)
        legal = venue_legal_sell_shares(remaining, self.lifecycle.policy.sell_size_increment)
        if not can_post_exit(legal_shares=legal, min_order_size=self._held_min_order_size()):
            return
        limit = self.lifecycle.planner.resting_take_profit(
            market_id=(
                self.active_market.market.condition_id
                or self.active_market.market.market_id.value
            ),
            instrument_id=self.selected_instrument.instrument_id.value,
            token_id=self.selected_token_id,
            shares=legal,
            limit_price=tp,
            time_in_force=time_in_force,
            expiration_epoch_s=expiration,
        )
        try:
            posted = await self.lifecycle.submit_resting_limit(limit)
        except DispatchBlocked as exc:
            armed.resting_fallback_reason = str(exc) or "post_only_would_cross"
            self.run_recorder.record(
                "RESTING_TP_FALLBACK",
                {"reason": armed.resting_fallback_reason},
            )
            return
        armed.resting_posted = True
        for order in posted.orders.values():
            if order.role is ExecutionRole.EXIT and order.venue_order_id:
                armed.resting_venue_order_id = order.venue_order_id
        self.run_recorder.record(
            "RESTING_TAKE_PROFIT_POSTED",
            {
                "session_id": state.session_id,
                "venue_order_id": armed.resting_venue_order_id,
                "limit_price": str(tp),
                "shares": str(legal),
                "time_in_force": time_in_force.value,
            },
        )

    def _watch_resting_take_profit(
        self, armed: ArmedProtection, state: ExecutionSessionState
    ) -> None:
        if not armed.resting_venue_order_id:
            return
        live = set(state.resting_limit_venue_ids) | set(state.open_venue_order_ids)
        if armed.resting_venue_order_id in live:
            return
        exit_order = state.exit
        filled = exit_order is not None and (
            exit_order.cumulative_matched_hwm > 0 or bool(exit_order.trade_ids_from_response)
        )
        armed.resting_venue_order_id = None
        armed.resting_posted = False
        if filled:
            return
        armed.resting_fallback_reason = "resting_order_disappeared"
        self.run_recorder.record(
            "RESTING_TP_WATCHDOG",
            {"reason": "disappeared_unfilled"},
        )
