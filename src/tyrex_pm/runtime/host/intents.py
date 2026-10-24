"""Intent queue and execution: entries, exits, outcomes back to the strategy."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from tyrex_pm.core.intents import (
    CancelIntent,
    EnterIntent,
    ExitIntent,
    FlattenIntent,
    HoldToResolutionIntent,
    LiquidityRole,
)
from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
)
from tyrex_pm.execution.coordinator import DispatchBlocked
from tyrex_pm.execution.exit_arbitration import admit_exit
from tyrex_pm.execution.outcome_reasons import classify_reason, classify_reasons
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
)
from tyrex_pm.runtime.decision_records import decision_context
from tyrex_pm.runtime.host.shared import (
    _hold_committed,
)
from tyrex_pm.runtime.run_config import (
    effective_entry_cutoff_s,
)
from tyrex_pm.strategies.outcomes import (
    EntryOutcome,
    EntryOutcomeKind,
    ExitOutcome,
    ExitOutcomeKind,
    RetryClass,
)


class IntentExecutionMixin:
    """Intent queue and execution: entries, exits, outcomes back to the strategy.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def _select_token(self, intent: EnterIntent) -> tuple[str, Any]:
        if self.active_market is None:
            raise RuntimeError("no active market")
        market = self.active_market.market
        for instrument in (market.yes, market.no):
            if instrument.instrument_id == intent.instrument_id:
                return instrument.token_id.value, instrument
        raise RuntimeError("entry intent is outside the active market")

    def _enqueue_intent(self, intent: Any) -> None:
        self.intent_candidate_monotonic_ns[intent.intent_id.value] = time.monotonic_ns()
        self.intent_queue.put_nowait(intent)
        self.intent_wake.set()

    def _start_intent_dispatcher(self) -> None:
        if self._intent_dispatcher_task is not None and not self._intent_dispatcher_task.done():
            return
        self._intent_dispatcher_stop.clear()
        self._intent_dispatcher_task = asyncio.create_task(
            self._intent_dispatcher_loop(),
            name="intent-dispatcher",
        )

    async def _stop_intent_dispatcher(self, *, cancel_inflight: bool = True) -> None:
        self._intent_dispatcher_stop.set()
        self.intent_wake.set()
        task = self._intent_dispatcher_task
        self._intent_dispatcher_task = None
        if task is None:
            return
        if cancel_inflight:
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _intent_dispatcher_loop(self) -> None:
        while not self._intent_dispatcher_stop.is_set():
            await self.intent_wake.wait()
            self.intent_wake.clear()
            while not self.intent_queue.empty() and not self._intent_dispatcher_stop.is_set():
                await self._consume_intent()

    async def _consume_intent(self) -> None:
        if self.intent_queue.empty():
            return
        intent = self.intent_queue.get_nowait()
        candidate_monotonic_ns = self.intent_candidate_monotonic_ns.get(intent.intent_id.value)
        try:
            if isinstance(intent, EnterIntent):
                await self._consume_entry(intent, candidate_monotonic_ns)
            elif isinstance(intent, (ExitIntent, FlattenIntent)):
                await self._consume_exit(intent, candidate_monotonic_ns)
            elif isinstance(intent, CancelIntent):
                state = await self.lifecycle.cancel_intent(intent)
                self.run_recorder.record(
                    "CANCEL_INTENT_CONSUMED",
                    {
                        "intent_id": intent.intent_id.value,
                        "order_id": intent.order_id.value,
                        "reason_code": intent.reason_code,
                    },
                )
                if state.terminal:
                    self.stop_requested = True
            elif isinstance(intent, HoldToResolutionIntent):
                await self._consume_hold(intent)
            else:
                raise TypeError(f"unsupported strategy intent: {type(intent).__name__}")
        except Exception as exc:  # noqa: BLE001 - persisted report owns run failure
            self._record_runtime_error("intent", exc)
        finally:
            self.intent_candidate_monotonic_ns.pop(intent.intent_id.value, None)
            self.intent_queue.task_done()

    @property
    def continuous(self) -> bool:
        market = getattr(self.config, "market", None)
        return getattr(market, "run_mode", "single_window") == "continuous"

    def _multi_attempt(self) -> bool:
        """Whether a terminal attempt may be followed by another one in this process."""
        strategy = getattr(self.config, "strategy", None)
        retry = getattr(strategy, "retry", None)
        reentry = getattr(strategy, "reentry", None)
        return bool(
            self.continuous
            or (retry is not None and getattr(retry, "enabled", False))
            or (reentry is not None and getattr(reentry, "max_trades_per_window", 1) > 1)
        )

    @staticmethod
    def _intent_episode(intent: Any) -> tuple[int, int]:
        evidence = getattr(intent, "evidence", {}) or {}
        return int(evidence.get("episode_seq") or 1), int(evidence.get("attempt_seq") or 1)

    def _attempt_session_id(self, intent: EnterIntent) -> str:
        assert self.stream_session_id is not None
        episode, attempt = self._intent_episode(intent)
        if episode <= 1 and attempt <= 1:
            return self.stream_session_id
        return f"{self.stream_session_id}:e{episode}a{attempt}"

    def _entry_latency(self, intent: EnterIntent, state: Any = None) -> dict[str, Any]:
        """Signal-to-submission timing and its components (milliseconds)."""
        now_ns = time.monotonic_ns()
        signal_ns = self.intent_candidate_monotonic_ns.get(intent.intent_id.value)
        submit_ns = self._entry_submit_started_ns.get(intent.intent_id.value)

        def ms(a: int | None, b: int | None) -> float | None:
            return None if a is None or b is None else round((b - a) / 1e6, 3)

        latency: dict[str, Any] = {
            "signal_to_submit_start_ms": ms(signal_ns, submit_ns),
            "submit_elapsed_ms": ms(submit_ns, now_ns),
            "signal_to_outcome_ms": ms(signal_ns, now_ns),
        }
        order = None if state is None else getattr(state, "entry", None)
        if order is not None:
            for key in ("refresh_wait_ms", "refresh_elapsed_ms", "prepare_queue_wait_ms",
                        "tick_resolve_ms", "create_market_order_ms",
                        "pre_dispatch_stage", "pre_dispatch_error_code"):
                latency[key] = getattr(order, key, None)
            # prepare/sign, final gate, durable authorization and the POST itself.
            stages = getattr(self.coordinator, "submit_timings", {}).get(
                getattr(order.spec, "order_id", None), {}
            )
            for key in ("prepare_ms", "final_gate_ms", "authorize_ms", "post_ms"):
                latency[key] = stages.get(key)
        monitor = self.loop_lag_monitor
        latency["event_loop_lag_recent_max_ms"] = (
            None if monitor is None else monitor.recent_max_ms()
        )
        gateway = self.gateway
        latency["account_reads_isolated"] = (
            None if gateway is None else getattr(gateway, "client_guard", None) is None
        )
        return latency

    def _deliver_entry_outcome(
        self, intent: EnterIntent, outcome: EntryOutcome, state: Any = None
    ) -> None:
        window_id = None if self.active_market is None else self.active_market.window_id
        payload = {
            "intent_id": intent.intent_id.value,
            "window_id": window_id,
            "episode_seq": self._intent_episode(intent)[0],
            "attempt_seq": self._intent_episode(intent)[1],
            "kind": outcome.kind.value,
            "reason": outcome.reason,
            "retry_class": outcome.retry_class.value,
            "posted": outcome.posted,
            "prepare_failed": outcome.prepare_failed,
            "confirmed_shares": str(outcome.confirmed_shares),
            "latency": self._entry_latency(intent, state),
        }
        self._entry_submit_started_ns.pop(intent.intent_id.value, None)
        self.run_recorder.record("ENTRY_OUTCOME", payload)
        if outcome.retry_class is RetryClass.PAUSE_ENTRIES:
            self.run_recorder.record(
                "ENTRIES_PAUSED_FOR_WINDOW",
                {"window_id": window_id, "reason": outcome.reason},
            )
        driver = self._driver_for(window_id)
        if driver is not None:
            driver.on_entry_outcome(outcome)
            self._capture_feedback(window_id, "entry", outcome)

    def _not_sent(self, intent: EnterIntent, reason: str, *, reasons: tuple[str, ...] = ()) -> None:
        retry_class = classify_reasons(reasons) if reasons else classify_reason(reason)
        self._deliver_entry_outcome(
            intent,
            EntryOutcome(
                EntryOutcomeKind.NOT_SENT,
                intent.intent_id.value,
                self._corrected_now(),
                reason=reason if not reasons else ",".join(reasons),
                retry_class=retry_class,
            ),
        )

    def _entry_outcome_from_state(
        self, intent: EnterIntent, state: ExecutionSessionState | None
    ) -> EntryOutcome:
        now = self._corrected_now()
        intent_id = intent.intent_id.value
        if state is None:
            return EntryOutcome(EntryOutcomeKind.UNCERTAIN, intent_id, now,
                                reason="DISPATCH_UNKNOWN",
                                retry_class=RetryClass.BLOCK_UNTIL_RECONCILED)
        entry = getattr(state, "entry", None)
        posted = bool(entry is not None and entry.attempt_ids)
        if getattr(state, "confirmed_position_shares", Decimal("0")) > 0:
            return EntryOutcome(EntryOutcomeKind.FILLED, intent_id, now, posted=True,
                                confirmed_shares=state.confirmed_position_shares,
                                cost=self._position_cost(state))
        if state.phase is ExecutionPhase.COMPLETED_NO_DISPATCH:
            stage = None if entry is None else entry.pre_dispatch_stage
            error = (None if entry is None else entry.last_error) or "NO_DISPATCH"
            if stage == "FINAL_GATE":
                reason = "FINAL_GATE_REJECTED:" + error.split(":", 1)[-1]
            else:
                reason = f"PREPARATION:{error}"
            return EntryOutcome(EntryOutcomeKind.NOT_SENT, intent_id, now, reason=reason,
                                retry_class=classify_reason(reason),
                                prepare_failed=stage != "FINAL_GATE")
        if state.phase is ExecutionPhase.COMPLETED_NO_FILL:
            reason = "NO_FILL_CONFIRMED"
            if entry is not None and entry.accepted is False:
                reason = "ORDER_REJECTED"
            return EntryOutcome(EntryOutcomeKind.ZERO_FILL, intent_id, now, reason=reason,
                                retry_class=classify_reason(reason), posted=posted)
        return EntryOutcome(EntryOutcomeKind.UNCERTAIN, intent_id, now, reason="DISPATCH_UNKNOWN",
                            retry_class=RetryClass.BLOCK_UNTIL_RECONCILED, posted=posted)

    def _release_attempt(self, state: ExecutionSessionState | None) -> None:
        """After a terminal, safe attempt: free the lifecycle for the next attempt."""
        if state is None or not getattr(state, "safe_to_open_entry", False):
            return
        self.lifecycle.release_terminal(keep_for_window=True)
        self.selected_instrument = None
        self.selected_token_id = None
        self._last_release_at = self._corrected_now()

    async def _consume_entry(self, intent: EnterIntent, candidate_monotonic_ns: int | None) -> None:
        token_id, instrument = self._select_token(intent)
        if self.active_market is None or self.stream_session_id is None:
            raise RuntimeError("active execution binding is not prepared")
        market = self.active_market.market
        market_id = market.condition_id or market.market_id.value
        unresolved = getattr(self.lifecycle, "unresolved_window_sessions", lambda: [])()
        if unresolved or getattr(self.lifecycle, "state", None) is not None:
            reason = "PRIOR_ATTEMPT_UNRESOLVED"
            self.run_recorder.record(
                "INTENT_BLOCKED",
                {
                    "intent_id": intent.intent_id.value,
                    "intent_type": type(intent).__name__,
                    "reason": reason,
                    "sessions": [s.session_id for s in unresolved],
                },
            )
            self._not_sent(intent, reason)
            return
        account_state = self.account_state_authority.current(market_id)
        self._apply_account_snapshot(account_state)
        released_at = self._last_release_at
        stale_after_release = (
            released_at is not None
            and self._intent_episode(intent)[0] > 1
            and (
                getattr(account_state, "observed_at", None) is None
                or account_state.observed_at < released_at
            )
        )
        if account_state.status is not AccountSnapshotStatus.READY or stale_after_release:
            self.run_recorder.record(
                "INTENT_BLOCKED",
                {
                    "intent_id": intent.intent_id.value,
                    "intent_type": type(intent).__name__,
                    "reason": "ACCOUNT_STATE_NOT_AUTHORITATIVE",
                    "account_state_status": account_state.status.value,
                    "account_state_blockers": list(account_state.blockers),
                    "stale_after_release": stale_after_release,
                },
            )
            self._not_sent(intent, "ACCOUNT_STATE_NOT_AUTHORITATIVE")
            return
        tau = self._current_entry_tau_s()
        cutoff_s = effective_entry_cutoff_s(self.config)
        if tau is None or tau < cutoff_s:
            self.run_recorder.record(
                "INTENT_BLOCKED",
                {
                    "intent_id": intent.intent_id.value,
                    "intent_type": type(intent).__name__,
                    "reason": "ENTRY_CUTOFF_PASSED",
                    "tau_s": tau,
                    "cutoff_s": cutoff_s,
                },
            )
            self._not_sent(intent, "ENTRY_CUTOFF_PASSED")
            return
        if self.settlement_hooks is not None:
            blocked = await self._settlement_before_entry(
                intent, token_id, instrument, account_state=account_state
            )
            if blocked is not None:
                self._not_sent(intent, "SETTLEMENT_ENTRY_BLOCKED", reasons=blocked)
                return
        baseline = account_state.token(token_id)
        self.account_state = account_state
        self.selected_token_id = token_id
        self.selected_instrument = instrument
        await self.lifecycle.open(
            session_id=self._attempt_session_id(intent),
            strategy_id=intent.strategy_id.value,
            market_id=self.active_market.market.condition_id or intent.market_id.value,
            window_id=self.active_market.window_id,
            token_id=token_id,
            baseline_position_shares=baseline.balance_shares,
            baseline_sellable_shares=min(
                baseline.balance_shares,
                baseline.allowance_shares
                if baseline.allowance_shares is not None
                else baseline.balance_shares,
            ),
            baseline_open_order_ids=account_state.open_order_ids,
        )
        try:
            async with self.account_state_authority.hold_ready_snapshot(market_id):
                # Re-apply held READY snapshot so final-gate capabilities
                # stay entry_executable for the full prepare window.
                self._apply_account_snapshot(self.account_state_authority.current(market_id))
                if not hasattr(self, "_entry_submit_started_ns"):
                    self._entry_submit_started_ns = {}
                self._entry_submit_started_ns[intent.intent_id.value] = time.monotonic_ns()
                state = await self.lifecycle.submit_entry(
                    intent,
                    token_id=token_id,
                    candidate_monotonic_ns=candidate_monotonic_ns,
                )
        except DispatchBlocked as exc:
            self.run_recorder.record(
                "DISPATCH_BLOCKED",
                {
                    "role": "ENTRY",
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            state = await self.lifecycle.reconcile()
        outcome = self._entry_outcome_from_state(intent, state)
        self._deliver_entry_outcome(intent, outcome, state)
        if self.settlement_hooks is not None and self.active_market is not None:
            episode_seq = self._intent_episode(intent)[0]
            settlement_outcome = self.settlement_hooks.after_entry(
                window_id=self.active_market.window_id,
                state=state,
                cost_basis=self._position_cost(state),
                episode_seq=episode_seq,
                posted=outcome.posted if self._multi_attempt() else None,
                prepare_failed=outcome.prepare_failed,
                **self._retry_clock_payload(),
            )
            self.run_recorder.record("SETTLEMENT_ENTRY_OUTCOME", {"outcome": settlement_outcome})
        if outcome.exposure and self._host_commits_hold() and self.active_market is not None:
            # hold_to_resolution: commit as soon as the fill is owned, not on the next
            # strategy evaluation (an account refresh in between must not leave it open).
            await self._commit_hold(self.active_market.window_id, source="host_after_fill",
                                    intent_id=intent.intent_id.value)
        if state is not None:
            self.capabilities.selected_token_sellable = state.sellable_shares > 0
            if intent.liquidity_role is LiquidityRole.MAKER and state.working_maker_entry:
                self.run_recorder.record(
                    "MAKER_LIMIT_POSTED",
                    {
                        "intent_id": intent.intent_id.value,
                        "token_id": token_id,
                        "venue_order_id": (
                            None if state.entry is None else state.entry.venue_order_id
                        ),
                    },
                )
            if state.phase is ExecutionPhase.POSITION_OPEN and self.active_market is not None:
                self._maybe_arm_protection(self.active_market)
                await self._maybe_post_resting_take_profit(self.active_market)
            if state.terminal:
                if self._multi_attempt() and getattr(state, "safe_to_open_entry", False):
                    self._release_attempt(state)
                elif self._multi_attempt():
                    # Unknown or manual outcome: no further BUY until reconciled.
                    self.capabilities.set_blocker(
                        "ENTRY_OUTCOME_UNRESOLVED", f"session {state.session_id}"
                    )
                    self.run_stop_reason = "MANUAL_INTERVENTION_REQUIRED"
                    self.stop_requested = True
                else:
                    self.stop_requested = True

    async def _consume_exit(
        self, intent: ExitIntent | FlattenIntent, candidate_monotonic_ns: int | None
    ) -> None:
        if self._recovery_policy_unresolved:
            self.run_recorder.record(
                "EXIT_INTENT_DROPPED",
                {
                    "intent_id": intent.intent_id.value,
                    "reason_code": intent.reason_code,
                    "dropped": "RECOVERY_POLICY_UNRESOLVED",
                },
            )
            return
        window_id = None if self.active_market is None else self.active_market.window_id
        state = self.lifecycle.state
        in_flight = None
        if state is not None and state.phase in (
            ExecutionPhase.EXIT_PREPARING,
            ExecutionPhase.EXIT_DISPATCHING,
        ):
            in_flight = state.exit_requested_reason or "EXIT"
        admission = admit_exit(in_flight_reason=in_flight, committed_hold=_hold_committed(self))
        if not admission.admitted or self.selected_token_id is None:
            self.run_recorder.record(
                "EXIT_INTENT_DROPPED",
                {
                    "intent_id": intent.intent_id.value,
                    "reason_code": intent.reason_code,
                    "dropped": admission.reason
                    if not admission.admitted
                    else "NO_SELECTED_TOKEN",
                },
            )
            self._deliver_exit_outcome(
                intent, ExitOutcomeKind.NOT_SENT, admission.reason, state, None
            )
            if self.selected_token_id is None and admission.admitted:
                raise RuntimeError("exit intent has no selected token")
            return
        market_id = None
        if self.active_market is not None:
            market = self.active_market.market
            market_id = market.condition_id or market.market_id.value
        before = None if state is None else state.confirmed_sell_shares
        before_trades = set() if state is None else set(state.trades)
        if window_id is not None:
            self._last_exit_reason[window_id] = intent.reason_code
        async with self.account_state_authority.hold_ready_snapshot(market_id or ""):
            if market_id is not None:
                self._apply_account_snapshot(self.account_state_authority.current(market_id))
            state = await self.lifecycle.submit_exit(
                intent,
                token_id=self.selected_token_id,
                protective=isinstance(intent, FlattenIntent),
                candidate_monotonic_ns=candidate_monotonic_ns,
                min_order_size=self._held_min_order_size(),
                last_bid=self._held_best_bid(self.active_market),
            )
        self._remember_dust(state)
        self.capabilities.selected_token_sellable = state.sellable_shares > 0
        flat = state.phase in (
            ExecutionPhase.COMPLETED_FLAT,
            ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
        )
        sold = state.confirmed_sell_shares - (before or Decimal("0"))
        if self.settlement_hooks is not None and window_id is not None:
            proceeds = Decimal("0")
            curve = self._strategy_fee_curve()
            from tyrex_pm.domain.polymarket.fees import phi_taker_fee_per_share

            for trade_id, trade in state.trades.items():
                if trade_id in before_trades or trade.side != "SELL":
                    continue
                fee = Decimal("0") if curve is None else trade.shares * phi_taker_fee_per_share(
                    trade.price, curve
                )
                proceeds += trade.shares * trade.price - fee
            recorded = self.settlement_hooks.after_exit(
                window_id=window_id,
                sold_shares=sold,
                proceeds=proceeds,
                flat=flat,
                exit_key=intent.intent_id.value,
            )
            self.run_recorder.record(
                "SETTLEMENT_EXIT_OUTCOME",
                {"outcome": recorded, "sold_shares": str(sold), "proceeds_estimated": str(proceeds),
                 "fee_basis": "strategy_fee_curve_estimate"},
            )
        if flat:
            kind = ExitOutcomeKind.FLAT
        elif state.phase is ExecutionPhase.MANUAL_INTERVENTION:
            kind = ExitOutcomeKind.UNCERTAIN
        elif sold > 0:
            kind = ExitOutcomeKind.PARTIAL
        else:
            kind = ExitOutcomeKind.NO_FILL
        self._deliver_exit_outcome(intent, kind, None, state, sold)
        if state.terminal:
            if flat and self._multi_attempt():
                self.protection_registry.disarm(state.session_id)
                self._release_attempt(state)
            else:
                self.stop_requested = True

    def _value_exit_check(self, spec):
        if (getattr(spec, "metadata", None) or {}).get("reason_code") != "VALUE_EXIT":
            return None
        runtime = self._market_runtime_ref
        if runtime is None:
            return "value_exit_runtime_missing"
        ready = runtime.prepare_aligned_eval()
        if not ready.ok or ready.snapshot is None:
            return "value_exit_inputs_unavailable"
        state = self.lifecycle.state
        context = decision_context(
            ready, run_id=self.config.run_name, target_notional=self.config.risk.target_notional,
            position_quantity=Decimal(0) if state is None else state.confirmed_position_shares)
        driver = runtime.window_driver()
        return None if driver is None else driver.value_exit_refusal(
            ready.snapshot, context, runtime.indicator_graph.snapshot(),
            spec.token_id, spec.minimum_price)

    def _deliver_exit_outcome(
        self,
        intent: ExitIntent | FlattenIntent,
        kind: ExitOutcomeKind,
        reason: str | None,
        state: ExecutionSessionState | None,
        sold: Decimal | None,
    ) -> None:
        window_id = None if self.active_market is None else self.active_market.window_id
        self.run_recorder.record(
            "EXIT_OUTCOME",
            {
                "intent_id": intent.intent_id.value,
                "reason_code": intent.reason_code,
                "kind": kind.value,
                "reason": reason,
                "sold_shares": None if sold is None else str(sold),
            },
        )
        driver = self._driver_for(window_id)
        if driver is not None:
            outcome = ExitOutcome(
                kind, intent.intent_id.value, self._corrected_now(), reason=reason,
                remaining_shares=Decimal("0") if state is None else state.confirmed_position_shares,
                sold_shares=sold or Decimal("0"))
            driver.on_exit_outcome(outcome)
            self._capture_feedback(window_id, "exit", outcome)
