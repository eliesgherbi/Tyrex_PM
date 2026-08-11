"""Event-driven entry-to-terminal execution lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    DispatchBlocked,
    PreDispatchFailure,
)
from tyrex_pm.execution.evidence import (
    ExecutionRole,
    ExitRequested,
    ManualInterventionRequired,
    SessionOpened,
    new_envelope,
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
    ) -> ExecutionSessionState:
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
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
            if state.phase is ExecutionPhase.COMPLETED_FLAT:
                return state
            if state.has_confirmed_sell and state.confirmed_trade_net_shares <= 0:
                # The session-owned trade ledger says the exit is complete;
                # wait for the account balance to converge instead of sending
                # a duplicate SELL against a lagging balance endpoint.
                await asyncio.sleep(self.policy.reconciliation_interval_s)
                continue
            shares = min(state.confirmed_position_shares, state.sellable_shares)
            if state.has_confirmed_buy:
                shares = min(
                    shares,
                    max(Decimal("0"), state.confirmed_trade_net_shares),
                )
            if shares <= 0:
                await asyncio.sleep(self.policy.reconciliation_interval_s)
                continue
            spec = self.planner.exit(
                intent,
                token_id=token_id,
                shares=shares,
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
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        state = self.coordinator.state(self.session_id)
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
