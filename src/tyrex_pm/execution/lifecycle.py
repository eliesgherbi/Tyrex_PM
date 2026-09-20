"""Event-driven entry-to-terminal execution lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.intents import CancelIntent, EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.execution.orders import LimitOrderSpec
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    CancelUnsupported,
    DispatchBlocked,
    PreDispatchFailure,
)
from tyrex_pm.execution.evidence import (
    DustResidualRecorded,
    ExecutionRole,
    ExitRequested,
    ManualInterventionRequired,
    SessionOpened,
    new_envelope,
)
from tyrex_pm.execution.exit_sizing import (
    can_post_exit,
    classify_dust_leftover,
    remaining_exit_shares,
    should_wait_before_exit_post,
    venue_legal_sell_shares,
)
from tyrex_pm.execution.planner import IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.runtime.capabilities import CapabilityController


@dataclass(frozen=True)
class LifecyclePolicy:
    evidence_timeout_s: float = 15.0
    reconciliation_interval_s: float = 0.25
    exit_retry_limit: int = 3
    exit_retry_budget_s: float = 30.0
    cancel_delay_s: float = 0.30
    cancel_retry_limit: int = 1
    sell_size_increment: Decimal = Decimal("0.01")
    dust_abs_shares: Decimal = Decimal("0.01")
    dust_max_notional: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        object.__setattr__(self, "sell_size_increment", Decimal(str(self.sell_size_increment)))
        object.__setattr__(self, "dust_abs_shares", Decimal(str(self.dust_abs_shares)))
        object.__setattr__(self, "dust_max_notional", Decimal(str(self.dust_max_notional)))
        if self.sell_size_increment <= 0:
            raise ValueError("sell_size_increment must be positive")
        if self.dust_abs_shares <= 0:
            raise ValueError("dust_abs_shares must be positive")
        if self.dust_max_notional <= 0:
            raise ValueError("dust_max_notional must be positive")


class ExecutionLifecycle:
    """Own one selected-token lifecycle; strategy only supplies intents."""

    def __init__(
        self,
        *,
        coordinator: AccountExecutionCoordinator,
        reconciler: SessionReconciler,
        planner: IntentOrderPlanner,
        capabilities: CapabilityController,
        policy: LifecyclePolicy | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.reconciler = reconciler
        self.planner = planner
        self.capabilities = capabilities
        self.policy = policy or LifecyclePolicy()
        self.session_id: str | None = None
        self._changed = asyncio.Event()
        self._gap_lock = asyncio.Lock()
        self.coordinator.listeners.append(self._on_state_changed)

    def _on_state_changed(self, state: ExecutionSessionState, _effects: tuple) -> None:
        if state.session_id == self.session_id:
            self._changed.set()

    @property
    def state(self) -> ExecutionSessionState | None:
        if self.session_id is None:
            return None
        return self.coordinator.states.get(self.session_id)

    async def open(
        self,
        *,
        session_id: str,
        strategy_id: str,
        market_id: str,
        window_id: str,
        token_id: str,
        baseline_position_shares: Decimal,
        baseline_sellable_shares: Decimal,
        baseline_open_order_ids: tuple[str, ...] = (),
    ) -> None:
        if self.session_id is not None:
            raise RuntimeError("execution lifecycle is already open")
        self.session_id = session_id
        await self.coordinator.apply(
            SessionOpened(
                **new_envelope(
                    session_id=session_id,
                    dedupe_key=f"session-opened:{session_id}",
                ),
                strategy_id=strategy_id,
                market_id=market_id,
                window_id=window_id,
                token_id=token_id,
                baseline_position_shares=baseline_position_shares,
                baseline_sellable_shares=baseline_sellable_shares,
                baseline_open_order_ids=baseline_open_order_ids,
            )
        )

    def resume(self, session_id: str) -> None:
        if self.session_id is not None:
            raise RuntimeError("execution lifecycle is already open")
        if session_id not in self.coordinator.states:
            raise RuntimeError("execution session was not restored")
        self.session_id = session_id

    def release_terminal(self) -> None:
        """Release a completed aggregate before binding the next market window."""
        state = self.state
        if state is None:
            return
        if not state.terminal:
            raise RuntimeError("cannot release a non-terminal execution session")
        self.session_id = None
        self._changed.clear()

    async def on_stream_evidence(self, evidence) -> None:
        if self.session_id is None or evidence.session_id != self.session_id:
            return
        await self.coordinator.apply(evidence)

    async def on_stream_ready(self) -> None:
        self.capabilities.user_stream_recovered()

    async def on_stream_gap(self, reason: str) -> None:
        self.capabilities.user_stream_gap()
        self.capabilities.set_blocker("STREAM_BACKFILL_IN_PROGRESS", reason)
        async with self._gap_lock:
            if self.session_id is not None:
                await self.reconciler.reconcile(self.coordinator, self.session_id)
        self.capabilities.clear_blocker("STREAM_BACKFILL_IN_PROGRESS")

    async def submit_entry(
        self,
        intent: EnterIntent,
        *,
        token_id: str,
        candidate_monotonic_ns: int | None = None,
    ) -> ExecutionSessionState:
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
        spec = self.planner.entry(
            intent,
            token_id=token_id,
            candidate_monotonic_ns=candidate_monotonic_ns,
        )
        try:
            await self.coordinator.submit(
                session_id=self.session_id,
                role=ExecutionRole.ENTRY,
                spec=spec,
            )
        except PreDispatchFailure:
            return self.coordinator.state(self.session_id)
        except Exception:  # submission ambiguity is already durable
            pass
        state = self.coordinator.state(self.session_id)
        if state.working_maker_entry:
            return state
        return await self._reconcile_until_entry_resolved()

    async def _reconcile_until_entry_resolved(self) -> ExecutionSessionState:
        assert self.session_id is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.evidence_timeout_s
        while True:
            await self.reconciler.reconcile(self.coordinator, self.session_id)
            state = self.coordinator.state(self.session_id)
            if state.phase in {
                ExecutionPhase.POSITION_OPEN,
                ExecutionPhase.COMPLETED_NO_DISPATCH,
                ExecutionPhase.COMPLETED_NO_FILL,
                ExecutionPhase.MANUAL_INTERVENTION,
            }:
                return state
            remaining = deadline - loop.time()
            if remaining <= 0:
                await self.coordinator.apply(
                    ManualInterventionRequired(
                        **new_envelope(
                            session_id=self.session_id,
                            dedupe_key="manual:entry_evidence_timeout",
                        ),
                        reason="entry_evidence_timeout",
                    )
                )
                return self.coordinator.state(self.session_id)
            self._changed.clear()
            try:
                await asyncio.wait_for(
                    self._changed.wait(),
                    timeout=min(self.policy.reconciliation_interval_s, remaining),
                )
            except TimeoutError:
                pass

    async def submit_exit(
        self,
        intent: ExitIntent | FlattenIntent,
        *,
        token_id: str,
        protective: bool = False,
        candidate_monotonic_ns: int | None = None,
        min_order_size: Decimal | None = None,
        last_bid: Decimal | None = None,
    ) -> ExecutionSessionState:
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
        await self._cancel_open_orders(reason=intent.reason_code)
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        state = self.coordinator.state(self.session_id)
        if state.phase is ExecutionPhase.MANUAL_INTERVENTION:
            return state
        if state.confirmed_position_shares <= 0:
            return state
        await self.coordinator.apply(
            ExitRequested(
                **new_envelope(
                    session_id=self.session_id,
                    dedupe_key=(f"exit-requested:{intent.intent_id.value}:{intent.reason_code}"),
                ),
                reason=intent.reason_code,
                protective=protective,
            )
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.exit_retry_budget_s
        attempts = 0
        while attempts < self.policy.exit_retry_limit and loop.time() < deadline:
            await self.reconciler.reconcile(self.coordinator, self.session_id)
            state = self.coordinator.state(self.session_id)
            if state.phase in {
                ExecutionPhase.COMPLETED_FLAT,
                ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
                ExecutionPhase.MANUAL_INTERVENTION,
            }:
                return state
            if should_wait_before_exit_post(state):
                await asyncio.sleep(self.policy.reconciliation_interval_s)
                continue
            remaining = remaining_exit_shares(state)
            legal = venue_legal_sell_shares(remaining, self.policy.sell_size_increment)
            if can_post_exit(legal_shares=legal, min_order_size=min_order_size):
                spec = self.planner.exit(
                    intent,
                    token_id=token_id,
                    shares=legal,
                    candidate_monotonic_ns=candidate_monotonic_ns,
                )
                try:
                    await self.coordinator.submit(
                        session_id=self.session_id,
                        role=ExecutionRole.EXIT,
                        spec=spec,
                    )
                except DispatchBlocked:
                    if not protective:
                        raise
                except Exception:  # ambiguous attempt is reconciled before retry
                    pass
                attempts += 1
                await asyncio.sleep(self.policy.reconciliation_interval_s)
                continue
            dust = classify_dust_leftover(
                remaining,
                increment=self.policy.sell_size_increment,
                min_order_size=min_order_size,
                last_bid=last_bid,
                dust_abs_shares=self.policy.dust_abs_shares,
                dust_max_notional=self.policy.dust_max_notional,
            )
            if dust is not None:
                return await self._record_dust(
                    state,
                    remaining=remaining,
                    classification=dust.code,
                    min_order_size=min_order_size,
                    last_bid=last_bid,
                )
            if state.has_confirmed_sell and state.confirmed_trade_net_shares <= 0:
                # Trade ledger is flat; wait for the account balance to converge.
                await asyncio.sleep(self.policy.reconciliation_interval_s)
                continue
            await asyncio.sleep(self.policy.reconciliation_interval_s)
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        state = self.coordinator.state(self.session_id)
        if state.phase in {
            ExecutionPhase.COMPLETED_FLAT,
            ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
            ExecutionPhase.MANUAL_INTERVENTION,
        }:
            return state
        remaining = remaining_exit_shares(state)
        dust = classify_dust_leftover(
            remaining,
            increment=self.policy.sell_size_increment,
            min_order_size=min_order_size,
            last_bid=last_bid,
            dust_abs_shares=self.policy.dust_abs_shares,
            dust_max_notional=self.policy.dust_max_notional,
        )
        if (
            dust is not None
            and not should_wait_before_exit_post(state)
        ):
            return await self._record_dust(
                state,
                remaining=remaining,
                classification=dust.code,
                min_order_size=min_order_size,
                last_bid=last_bid,
            )
        if state.confirmed_position_shares > 0:
            await self.coordinator.apply(
                ManualInterventionRequired(
                    **new_envelope(
                        session_id=self.session_id,
                        dedupe_key="manual:exit_retry_budget_exhausted",
                    ),
                    reason="exit_retry_budget_exhausted",
                )
            )
            state = self.coordinator.state(self.session_id)
        return state

    async def submit_resting_limit(self, spec: LimitOrderSpec) -> ExecutionSessionState:
        """POST a sitting EXIT limit without marking the session as exiting."""
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
        try:
            await self.coordinator.submit(
                session_id=self.session_id,
                role=ExecutionRole.EXIT,
                spec=spec,
            )
        except PreDispatchFailure:
            return self.coordinator.state(self.session_id)
        except Exception:
            pass
        return self.coordinator.state(self.session_id)

    async def cancel_intent(self, intent: CancelIntent) -> ExecutionSessionState:
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
        await self.coordinator.cancel(
            session_id=self.session_id,
            venue_order_ids=(intent.order_id.value,),
            reason=intent.reason_code,
        )
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        return self.coordinator.state(self.session_id)

    async def _cancel_open_orders(self, *, reason: str) -> ExecutionSessionState:
        assert self.session_id is not None
        state = self.coordinator.state(self.session_id)
        ids = state.resting_limit_venue_ids
        if not ids:
            return state
        attempts = 0
        while True:
            try:
                result = await self.coordinator.cancel(
                    session_id=self.session_id,
                    venue_order_ids=ids,
                    reason=reason,
                )
            except CancelUnsupported:
                await self.mark_manual("cancel_unsupported")
                return self.coordinator.state(self.session_id)
            except Exception:
                await self.mark_manual("cancel_failed")
                return self.coordinator.state(self.session_id)
            if result.delay_pending and attempts < self.policy.cancel_retry_limit:
                attempts += 1
                await asyncio.sleep(self.policy.cancel_delay_s)
                await self.reconciler.reconcile(self.coordinator, self.session_id)
                state = self.coordinator.state(self.session_id)
                ids = state.resting_limit_venue_ids
                if not ids:
                    return state
                continue
            break
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        state = self.coordinator.state(self.session_id)
        if state.resting_limit_venue_ids:
            await self.mark_manual("cancel_still_live")
            return self.coordinator.state(self.session_id)
        return state

    async def _record_dust(
        self,
        state: ExecutionSessionState,
        *,
        remaining: Decimal,
        classification: str,
        min_order_size: Decimal | None,
        last_bid: Decimal | None,
    ) -> ExecutionSessionState:
        assert self.session_id is not None
        identity = state.identity
        if identity is None:
            raise RuntimeError("cannot record dust on an unopened session")
        notional = None if last_bid is None else remaining * Decimal(str(last_bid))
        await self.coordinator.apply(
            DustResidualRecorded(
                **new_envelope(
                    session_id=self.session_id,
                    dedupe_key=f"dust:{identity.token_id}:{remaining}",
                ),
                token_id=identity.token_id,
                shares=remaining,
                market_id=identity.market_id,
                window_id=identity.window_id,
                classification=classification,
                last_bid=last_bid,
                min_order_size=min_order_size,
                sell_size_increment=self.policy.sell_size_increment,
                notional=notional,
                reason="classified_dust",
            )
        )
        return self.coordinator.state(self.session_id)

    async def reconcile(self) -> ExecutionSessionState | None:
        if self.session_id is None:
            return None
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        return self.coordinator.state(self.session_id)

    async def mark_manual(self, reason: str) -> ExecutionSessionState:
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
        await self.coordinator.apply(
            ManualInterventionRequired(
                **new_envelope(
                    session_id=self.session_id,
                    dedupe_key=f"manual:{reason}",
                ),
                reason=reason,
            )
        )
        return self.coordinator.state(self.session_id)
