"""Event-driven entry-to-terminal execution lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.intents import CancelIntent, EnterIntent, ExitIntent, FlattenIntent
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
from tyrex_pm.execution.orders import LimitOrderSpec
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
        # The user stream stamps evidence with the stream (binding) session id. With
        # several attempts per window each attempt has its own execution session; the
        # stream id is an alias routed to the session that owns the order.
        self.stream_alias: str | None = None
        # Earlier sessions of the current window (released attempts, sold episodes),
        # kept for evidence routing, fill attribution and late-fill detection.
        self.window_sessions: list[str] = []
        # Sessions holding a committed settlement position from an earlier window.
        self.detached_sessions: list[str] = []
        # Event-loop time each window session was released (late-evidence watch).
        self.released_at: dict[str, float] = {}
        self._last_watch: dict[str, float] = {}
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

    def release_terminal(self, *, keep_for_window: bool = False) -> None:
        """Release a completed aggregate (next attempt, next episode or next window)."""
        state = self.state
        if state is None:
            return
        if not state.safe_to_open_entry:
            raise RuntimeError("cannot release a session that is not safe to open entry")
        if keep_for_window and self.session_id not in self.window_sessions:
            self.window_sessions.append(self.session_id)
            self.released_at[self.session_id] = asyncio.get_running_loop().time()
        self.session_id = None
        self._changed.clear()

    def detach_committed(self, *, reason: str) -> str:
        """Release a session whose remaining inventory is a committed settlement hold.

        Allowed only without live orders, unresolved mutations or unconfirmed matches:
        the position is then intentional settlement exposure owned by the ledger, not
        an open trading lifecycle. Returns the detached session id.
        """
        state = self.state
        if state is None or self.session_id is None:
            raise RuntimeError("no session to detach")
        if (
            state.resting_limit_venue_ids
            or state.has_unresolved_mutation_attempt
            or state.entry_match_awaiting_confirmation
            or state.has_pending_trades
            or state.phase is ExecutionPhase.MANUAL_INTERVENTION
        ):
            raise RuntimeError(f"cannot detach a session with pending work ({reason})")
        detached = self.session_id
        self.detached_sessions.append(detached)
        self.session_id = None
        self._changed.clear()
        return detached

    def new_window(self) -> None:
        """Forget the previous window's attempt sessions (they are terminal and safe)."""
        self.window_sessions = []
        self.released_at = {}
        self._last_watch = {}

    def sessions_to_watch(self, *, watch_s: float, interval_s: float) -> list[str]:
        """Released attempt sessions still inside their late-evidence watch, due a poll."""
        now = asyncio.get_running_loop().time()
        due = []
        for session_id in self.window_sessions:
            released = self.released_at.get(session_id)
            if released is None or now - released > watch_s:
                continue
            if now - self._last_watch.get(session_id, 0.0) < interval_s:
                continue
            self._last_watch[session_id] = now
            due.append(session_id)
        return due

    def unresolved_window_sessions(self) -> list[ExecutionSessionState]:
        """Earlier attempt sessions of this window that late evidence made unsafe."""
        out = []
        for session_id in self.window_sessions:
            state = self.coordinator.states.get(session_id)
            if state is not None and not state.safe_to_open_entry:
                out.append(state)
        return out

    def _route(self, evidence) -> str | None:
        if self.session_id is not None and evidence.session_id == self.session_id:
            return self.session_id
        if self.stream_alias is None or evidence.session_id != self.stream_alias:
            return None
        venue_id = getattr(evidence, "venue_order_id", None)
        order_id = getattr(evidence, "order_id", None)
        candidates = ([self.session_id] if self.session_id else []) + list(
            reversed(self.window_sessions)
        )
        for session_id in candidates:
            state = self.coordinator.states.get(session_id)
            if state is None:
                continue
            for order in state.orders.values():
                if (venue_id and order.venue_order_id == venue_id) or (
                    order_id and order.spec.order_id == order_id
                ):
                    return session_id
        return self.session_id

    async def on_stream_evidence(self, evidence) -> None:
        target = self._route(evidence)
        if target is None:
            return
        if target != evidence.session_id:
            import dataclasses

            evidence = dataclasses.replace(evidence, session_id=target)
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
        current = self.coordinator.state(self.session_id)
        if current.phase is ExecutionPhase.MANUAL_INTERVENTION:
            raise RuntimeError("recovery cannot request a BUY")
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
        historical_recovery: bool = False,
    ) -> ExecutionSessionState:
        if self.session_id is None:
            raise RuntimeError("execution lifecycle is not open")
        await self._cancel_open_orders(reason=intent.reason_code)
        await self.reconciler.reconcile(self.coordinator, self.session_id)
        state = self.coordinator.state(self.session_id)
        recovery = historical_recovery or state.phase is ExecutionPhase.MANUAL_INTERVENTION
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
            }:
                return state
            if state.phase is ExecutionPhase.MANUAL_INTERVENTION:
                if not recovery or state.confirmed_position_shares <= 0:
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
                        recovery=recovery,
                        historical_recovery=historical_recovery,
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

    def fill_context(self) -> tuple[set[str], str | None, dict[str, str]]:
        """Owned venue orders of the current session and the window's earlier attempts."""
        state = self.state
        sessions = [state] if state is not None else []
        sessions += [
            self.coordinator.states[s] for s in self.window_sessions if s in self.coordinator.states
        ]
        if not sessions:
            return set(), None, {}
        owned: set[str] = set()
        sides: dict[str, str] = {}
        for session in sessions:
            for order in session.orders.values():
                if order.venue_order_id:
                    owned.add(order.venue_order_id)
                    sides[order.venue_order_id] = order.spec.side.value
        reference = state or sessions[0]
        token_id = None if reference.identity is None else reference.identity.token_id
        return owned, token_id, sides

    async def drain_open_session(
        self,
        reason: str,
        *,
        min_order_size: Decimal | None = None,
        last_bid: Decimal | None = None,
    ) -> ExecutionSessionState | None:
        """Cancel, prove, and flatten while gateway/books are still available."""
        if self.session_id is None:
            return None
        state = self.coordinator.state(self.session_id)
        if state.safe_to_open_entry and not state.needs_venue_followup:
            return state
        await self._cancel_open_orders(reason=reason)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.policy.evidence_timeout_s
        while loop.time() < deadline:
            await self.reconciler.reconcile(self.coordinator, self.session_id)
            state = self.coordinator.state(self.session_id)
            if state.phase in {
                ExecutionPhase.COMPLETED_NO_FILL,
                ExecutionPhase.COMPLETED_FLAT,
                ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
                ExecutionPhase.COMPLETED_NO_DISPATCH,
            }:
                return state
            if (
                state.phase is ExecutionPhase.MANUAL_INTERVENTION
                and not state.has_exposure
                and not state.working_maker_entry
                and not state.resting_limit_venue_ids
                and not state.has_unresolved_mutation_attempt
            ):
                return state
            if state.has_exposure:
                from datetime import datetime, timezone

                from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
                from tyrex_pm.core.intents import FlattenIntent, new_intent_id

                identity = state.identity
                if identity is None:
                    break
                await self.submit_exit(
                    FlattenIntent(
                        intent_id=new_intent_id(),
                        strategy_id=StrategyId(identity.strategy_id),
                        instrument_id=InstrumentId(identity.token_id),
                        market_id=MarketId(identity.market_id),
                        created_at=datetime.now(timezone.utc),
                        correlation_id=CorrelationId("drain"),
                        causation_id=None,
                        reason_code=reason,
                    ),
                    token_id=identity.token_id,
                    protective=True,
                    min_order_size=min_order_size,
                    last_bid=last_bid,
                )
                # MANUAL remains terminal after recovery events. Keep looping
                # while exposure, live orders, or unresolved mutations remain.
            await asyncio.sleep(self.policy.reconciliation_interval_s)
        state = self.coordinator.state(self.session_id)
        if not state.safe_to_open_entry:
            return await self.mark_manual(f"drain_incomplete:{reason}")
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
