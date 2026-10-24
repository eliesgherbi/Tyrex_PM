"""Periodic tick: deadlines, mandatory flatten, residual hold, reconcile."""

from __future__ import annotations

from datetime import datetime, timezone

from tyrex_pm.core.intents import (
    FlattenIntent,
    new_intent_id,
)
from tyrex_pm.runtime.host.shared import (
    _hold_committed,
    ready_binding_strategy_id,
    ready_fallback_correlation,
)
from tyrex_pm.runtime.market_runtime import ZGapMarketRuntime
from tyrex_pm.runtime.run_config import (
    entry_tau_bounds,
    strategy_entry_cutoff_s,
)


class DeadlineMixin:
    """Periodic tick: deadlines, mandatory flatten, residual hold, reconcile.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    async def on_async_tick(self, market_runtime: ZGapMarketRuntime) -> None:
        pending_hold = self._pending_hold_commit
        if pending_hold is not None:
            await self._commit_hold(pending_hold, source="host_after_fill")
        if self._pending_detach is not None:
            await self._try_detach()
        await self._bind_pending_market()
        if self.decision_recording is not None:
            self._check_raw_capture()
            self._recording_heartbeat()
        worker = self.settlement_worker
        if worker is not None:
            worker.start()  # idempotent; also covers a restart into a committed window
        if self.active_market is not None:
            market = self.active_market.market
            market_id = market.condition_id or market.market_id.value
            self._apply_account_snapshot(self.account_state_authority.current(market_id))
        self._market_data_ready()
        graph = getattr(market_runtime, "indicator_graph", None)
        if graph is not None:
            self.indicator_graph = graph
        if self.active_market is not None and self.active_market.market.event_end is not None:
            tau = self._current_entry_tau_s()
            _tau_min_s, tau_max_s = entry_tau_bounds(self.config)
            # Soft close only. Consume and the final BUY gate still use the hard cutoff.
            soft_cutoff_s = strategy_entry_cutoff_s(self.config)
            self.capabilities.entry_window_open = bool(
                tau is not None
                and self.active_market.entry_enabled
                and soft_cutoff_s <= tau <= tau_max_s
            )
            state = self.lifecycle.state
            remaining = float("-inf") if tau is None else tau
            if _hold_committed(self):
                # Committed to resolution: never sold before close by the host.
                self._maybe_refresh_active_order_metadata()
                return
            strategy_exit = getattr(self.config.strategy, "exit", None)
            residual_hold = (getattr(self.config, "lifecycle_mode", None) == "managed_exit"
                             and getattr(strategy_exit, "residual_policy", None)
                             == "hold_to_resolution")
            if (residual_hold and state is not None and state.has_exposure
                    and remaining <= strategy_exit.exit_cutoff_tau_s):
                committed = await self._commit_hold(
                    self.active_market.window_id, source="host_exit_cutoff")
                if (not committed
                        and remaining <= self.config.lifecycle.manual_deadline_before_end_s):
                    await self.lifecycle.mark_manual("residual_hold_not_proven_at_deadline")
                    self.stop_requested = True
                return
            if (
                state is not None
                and state.has_exposure
                and remaining <= self.config.lifecycle.manual_deadline_before_end_s
            ):
                await self.lifecycle.mark_manual("manual_deadline_reached_with_exposure")
                self.stop_requested = True
                return
            if (
                state is not None
                and state.has_exposure
                and remaining <= self.config.lifecycle.mandatory_exit_before_end_s
                and not residual_hold
                and not state.exit_requested_reason
                and self.selected_instrument is not None
            ):
                self._enqueue_intent(
                    FlattenIntent(
                        intent_id=new_intent_id(),
                        strategy_id=ready_binding_strategy_id(market_runtime),
                        instrument_id=self.selected_instrument.instrument_id,
                        market_id=self.active_market.market.market_id,
                        created_at=datetime.now(timezone.utc),
                        correlation_id=market_runtime.prepare_aligned_eval().snapshot.correlation_id
                        if market_runtime.prepare_aligned_eval().snapshot is not None
                        else ready_fallback_correlation(),
                        causation_id=None,
                        reason_code="MANDATORY_FLATTEN",
                    )
                )
            self._maybe_arm_protection(market_runtime)
            await self._maybe_post_resting_take_profit(market_runtime)
            if remaining > self.config.lifecycle.mandatory_exit_before_end_s:
                self._maybe_emit_protection_exit(market_runtime)
        # Intent drain is owned by the event-woken intent dispatcher so entry
        # does not wait for the 1 Hz market-data tick. Tick still refreshes
        # readiness, refreshes order metadata before expiry and reconciles open exposure.
        self._maybe_refresh_active_order_metadata()
        state = self.lifecycle.state
        if state is not None and state.needs_venue_followup:
            reconciled = await self.lifecycle.reconcile()
            if reconciled is not None:
                self.capabilities.selected_token_sellable = reconciled.sellable_shares > 0
        check_earlier = self._check_earlier_attempts
        if check_earlier is not None:
            await check_earlier()
