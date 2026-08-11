"""The single production orchestration path from market data to terminal state."""

from __future__ import annotations

import asyncio
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.ids import RunId
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, new_intent_id
from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
    AccountStateAuthority,
    AccountStatePolicy,
    AccountStateSnapshot,
)
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator, DispatchBlocked
from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
)
from tyrex_pm.execution.final_gate import FinalExecutionGate, FinalGatePolicy
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase, ExecutionSessionState
from tyrex_pm.market_data.book_health import ConnectionHealth, FeedSyncPhase, SyncHealth
from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal
from tyrex_pm.persistence.run_evidence_journal import (
    RunEvidenceRecord,
    RunEvidenceRecorder,
    SqliteRunEvidenceJournal,
)
from tyrex_pm.reporting.run_report import (
    RunReportInput,
    build_run_report,
    write_emergency_report,
    write_run_report,
)
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.market_data_runtime import MarketDataSummary, run_market_data_runtime
from tyrex_pm.runtime.market_runtime import ZGapMarketRuntime
from tyrex_pm.runtime.run_config import TradingRunConfig
from tyrex_pm.strategies.context import (
    DecisionContext,
    StrategyLifecycleSnapshot,
    StrategyPositionPhase,
)


def load_dotenv_values(path: Path | None) -> dict[str, str]:
    values = dict(os.environ)
    if path is None or not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _lifecycle_view(
    state: ExecutionSessionState | None, *, instrument_id=None
) -> StrategyLifecycleSnapshot:
    phase = ExecutionPhase.FLAT if state is None else state.phase
    mapping = {
        ExecutionPhase.NEW: StrategyPositionPhase.FLAT,
        ExecutionPhase.FLAT: StrategyPositionPhase.FLAT,
        ExecutionPhase.ENTRY_PREPARING: StrategyPositionPhase.ENTRY_PENDING,
        ExecutionPhase.ENTRY_DISPATCHING: StrategyPositionPhase.ENTRY_PENDING,
        ExecutionPhase.POSITION_PENDING_CONFIRMATION: StrategyPositionPhase.ENTRY_PENDING,
        ExecutionPhase.POSITION_OPEN: StrategyPositionPhase.ACTIVE,
        ExecutionPhase.EXIT_PREPARING: StrategyPositionPhase.EXIT_REQUESTED,
        ExecutionPhase.EXIT_DISPATCHING: StrategyPositionPhase.EXIT_PENDING,
        ExecutionPhase.RECONCILING: (
            StrategyPositionPhase.ACTIVE
            if state is not None and state.has_exposure
            else StrategyPositionPhase.FLAT
        ),
        ExecutionPhase.COMPLETED_NO_DISPATCH: StrategyPositionPhase.FLAT,
        ExecutionPhase.COMPLETED_NO_FILL: StrategyPositionPhase.FLAT,
        ExecutionPhase.COMPLETED_FLAT: StrategyPositionPhase.FLAT,
        ExecutionPhase.MANUAL_INTERVENTION: StrategyPositionPhase.MANUAL_INTERVENTION,
    }
    return StrategyLifecycleSnapshot(
        phase=mapping[phase],
        instrument_id=instrument_id,
    )


@dataclass(frozen=True)
class TradingRunResult:
    outcome: str
    ok: bool
    report_path: Path
    mutation_attempts: int | None
    state: ExecutionSessionState | None


@dataclass
class TradingRuntime:
    config: TradingRunConfig
    output_directory: Path
    gateway: PolymarketAsyncGateway
    capabilities: CapabilityController
    coordinator: AccountExecutionCoordinator
    lifecycle: ExecutionLifecycle
    account_state_authority: AccountStateAuthority
    final_gate: FinalExecutionGate
    run_instance_id: str
    run_recorder: RunEvidenceRecorder
    decisions: list[dict[str, Any]] = field(default_factory=list)
    runtime_errors: list[str] = field(default_factory=list)
    market_summary: MarketDataSummary | None = None
    feed_supervisor: Any | None = None
    pending_market: Any | None = None
    active_market: Any | None = None
    account_state: AccountStateSnapshot | None = None
    stream_session_id: str | None = None
    selected_instrument: Any | None = None
    selected_token_id: str | None = None
    intent_queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    seen_intents: set[str] = field(default_factory=set)
    intent_candidate_monotonic_ns: dict[str, int] = field(default_factory=dict)
    stop_requested: bool = False
    restored_sessions: list[ExecutionSessionState] = field(default_factory=list)
    execution_timeline: list[dict[str, Any]] = field(default_factory=list)
    _last_readiness_key: tuple[Any, ...] | None = None

    @classmethod
    async def create(
        cls,
        *,
        config: TradingRunConfig,
        output_directory: Path,
        env: dict[str, str],
        gateway: PolymarketAsyncGateway | None = None,
        run_instance_id: str | None = None,
    ) -> "TradingRuntime":
        output_directory.mkdir(parents=True, exist_ok=True)
        active_gateway = gateway or await PolymarketAsyncGateway.from_env(env)
        capabilities = CapabilityController(
            live_requested=True,
            kill_switch_active=config.risk.kill_switch_active,
        )
        holder: dict[str, Any] = {"runtime": None}

        def capture_book():
            runtime = holder["runtime"]
            if runtime is None or runtime.feed_supervisor is None:
                return None
            return runtime.feed_supervisor.active_view()

        def binding_id() -> str | None:
            runtime = holder["runtime"]
            feed = None if runtime is None else runtime.feed_supervisor
            return None if feed is None or feed.active is None else feed.active.binding.binding_id

        def tick_size(token_id: str) -> Decimal | None:
            """Local cascade: live BookView → store/binding metadata (no I/O)."""
            runtime = holder["runtime"]
            view = capture_book()
            if view is not None:
                for leg in (view.up, view.down):
                    if leg.token_id == token_id and leg.tick_size is not None:
                        return leg.tick_size
            feed_supervisor = None if runtime is None else runtime.feed_supervisor
            if feed_supervisor is not None:
                return feed_supervisor.resolve_tick_size(token_id)
            return None

        async def fetch_tick_size(token_id: str) -> Decimal | None:
            """One-shot public book fetch when local tick metadata is still missing."""
            runtime = holder["runtime"]
            feed_supervisor = None if runtime is None else runtime.feed_supervisor
            if feed_supervisor is None:
                return None
            local = feed_supervisor.resolve_tick_size(token_id)
            if local is not None:
                return local
            active = feed_supervisor.active
            if active is None or not active.binding.owns_token(token_id):
                return None
            try:
                from tyrex_pm.adapters.polymarket.rest_book import fetch_clob_book

                payload = await asyncio.to_thread(fetch_clob_book, token_id)
            except Exception:
                return feed_supervisor.resolve_tick_size(token_id)
            if payload.tick_size is not None:
                feed_supervisor.store.seed_trading_parameters(
                    token_id,
                    tick_size=payload.tick_size,
                    min_order_size=payload.min_order_size,
                    binding_id=active.binding.binding_id,
                )
            else:
                active.seed_binding_trading_parameters()
            return feed_supervisor.resolve_tick_size(token_id)

        active_gateway.tick_size_resolver = tick_size
        active_gateway.tick_size_fetcher = fetch_tick_size

        final_gate = FinalExecutionGate(
            capture_book=capture_book,
            capabilities=capabilities,
            active_binding_id=binding_id,
            policy=FinalGatePolicy(
                max_book_age_ms=config.market.maximum_book_age_ms,
                max_candidate_age_ms=config.market.maximum_candidate_age_ms,
            ),
        )
        journal_path = config.state_directory / "execution.sqlite3"
        active_run_instance_id = run_instance_id or str(uuid4())
        journal = SqliteExecutionJournal(journal_path)
        run_recorder = RunEvidenceRecorder(
            SqliteRunEvidenceJournal(journal_path, run_instance_id=active_run_instance_id)
        )
        await run_recorder.start()
        coordinator = AccountExecutionCoordinator(
            journal=journal,
            gateway=active_gateway,
            final_gate=final_gate,
        )
        restored_sessions: list[ExecutionSessionState] = []
        for session_id in journal.sessions():
            restored = await coordinator.restore(session_id)
            if not restored.terminal:
                restored_sessions.append(restored)
        lifecycle = ExecutionLifecycle(
            coordinator=coordinator,
            reconciler=SessionReconciler(active_gateway),
            planner=IntentOrderPlanner(
                ExecutionRiskPolicy(
                    maximum_total_debit=config.risk.maximum_total_debit,
                    fee_reserve_rate=config.risk.fee_reserve_rate,
                    minimum_exit_price=config.lifecycle.minimum_exit_price,
                )
            ),
            capabilities=capabilities,
            policy=LifecyclePolicy(
                evidence_timeout_s=config.lifecycle.evidence_timeout_s,
                reconciliation_interval_s=config.lifecycle.reconciliation_interval_s,
                exit_retry_limit=config.lifecycle.exit_retry_limit,
                exit_retry_budget_s=config.lifecycle.exit_retry_budget_s,
            ),
        )
        result = cls(
            config=config,
            output_directory=output_directory,
            gateway=active_gateway,
            capabilities=capabilities,
            coordinator=coordinator,
            lifecycle=lifecycle,
            account_state_authority=AccountStateAuthority(
                active_gateway,
                policy=AccountStatePolicy(
                    refresh_interval_s=config.account.refresh_interval_s,
                    snapshot_max_age_s=config.account.snapshot_max_age_s,
                    read_timeout_s=config.account.read_timeout_s,
                    retry_attempts=config.account.retry_attempts,
                    retry_base_delay_s=config.account.retry_base_delay_s,
                ),
                evidence_sink=run_recorder.record,
            ),
            final_gate=final_gate,
            run_instance_id=active_run_instance_id,
            run_recorder=run_recorder,
            restored_sessions=restored_sessions,
        )
        holder["runtime"] = result
        run_recorder.record(
            "RUN_STARTED",
            {
                "configured_run_name": config.run_name,
                "strategy_kind": config.strategy_kind,
                "output_directory": str(output_directory),
                "live_requested": True,
            },
        )
        return result

    def on_feed_supervisor(self, supervisor: Any) -> None:
        self.feed_supervisor = supervisor
        self.run_recorder.record("PUBLIC_FEED_SUPERVISOR_READY", {})

    def on_active_market(self, market_session: Any) -> None:
        self.pending_market = market_session
        market = market_session.market
        self._prepare_account_state(market_session, active=True)
        self.run_recorder.record(
            "MARKET_ACTIVATION_REQUESTED",
            {
                "market_id": market.market_id.value,
                "condition_id": market.condition_id,
                "window_id": market_session.window_id,
                "entry_enabled": market_session.entry_enabled,
                "event_start": market.event_start,
                "event_end": market.event_end,
            },
        )

    def on_prepared_market(self, market_session: Any) -> None:
        """Prewarm next-market account state before its promotion boundary."""
        self._prepare_account_state(market_session, active=False)
        market = market_session.market
        self.run_recorder.record(
            "PREPARED_MARKET_ACCOUNT_REGISTERED",
            {
                "market_id": market.market_id.value,
                "condition_id": market.condition_id,
                "window_id": market_session.window_id,
            },
        )

    def _prepare_account_state(self, market_session: Any, *, active: bool) -> None:
        market = market_session.market
        self.account_state_authority.prepare(
            market_id=market.condition_id or market.market_id.value,
            token_ids=(market.yes.token_id.value, market.no.token_id.value),
            required_collateral=self.config.risk.maximum_total_debit,
            active=active,
        )

    def can_promote(self) -> bool:
        state = self.lifecycle.state
        return state is None or state.terminal

    def _market_data_ready(self) -> bool:
        feed = None if self.feed_supervisor is None else self.feed_supervisor.active
        view = None if self.feed_supervisor is None else self.feed_supervisor.active_view()
        ready = bool(
            feed is not None
            and feed.phase is FeedSyncPhase.READY
            and feed.connection_health is ConnectionHealth.LIVE
            and view is not None
            and view.up.sync_health is SyncHealth.READY
            and view.down.sync_health is SyncHealth.READY
        )
        self.capabilities.public_feeds_ready = ready
        self.capabilities.books_ready = ready
        self.capabilities.book_desync_active = not ready
        effective = ready and self.capabilities.active_market_ready
        self._record_readiness_if_changed()
        return effective

    def _entry_evaluation_ready(self) -> bool:
        """Refresh all synchronous capability projections before evaluation."""
        if self.active_market is not None:
            market = self.active_market.market
            market_id = market.condition_id or market.market_id.value
            self._apply_account_snapshot(self.account_state_authority.current(market_id))
        return self._market_data_ready()

    def _record_readiness_if_changed(self) -> None:
        state = self.lifecycle.state
        snapshot = self.capabilities.snapshot(exposed=bool(state and state.has_exposure))
        payload = {
            "observable": snapshot.observable,
            "decision_ready": snapshot.decision_ready,
            "entry_executable": snapshot.entry_executable,
            "execution_infrastructure_ready": snapshot.execution_infrastructure_ready,
            "exit_executable": snapshot.exit_executable,
            "reconciliation_ready": snapshot.reconciliation_ready,
            "blockers": list(snapshot.blockers),
        }
        key = (
            snapshot.observable,
            snapshot.decision_ready,
            snapshot.execution_infrastructure_ready,
            snapshot.entry_executable,
            snapshot.exit_executable,
            snapshot.reconciliation_ready,
            snapshot.blockers,
        )
        if key != self._last_readiness_key:
            self._last_readiness_key = key
            self.run_recorder.record("READINESS_CHANGED", payload)

    def _record_runtime_error(self, stage: str, exc: BaseException) -> None:
        message = f"{stage}:{type(exc).__name__}:{exc}"
        self.runtime_errors.append(message)
        chain: list[dict[str, str]] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(
                {
                    "error_type": type(current).__name__,
                    "message": str(current),
                }
            )
            current = current.__cause__ or current.__context__
        self.run_recorder.record(
            "RUNTIME_ERROR",
            {
                "stage": stage,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "exception_chain": chain,
                "traceback": "".join(traceback.format_exception(exc)),
            },
        )

    def _apply_account_snapshot(self, snapshot: AccountStateSnapshot) -> None:
        """Apply one immutable account observation on the runtime's main loop."""
        self.account_state = snapshot
        complete = snapshot.read_complete
        self.capabilities.account_reads_ready = complete
        self.capabilities.collateral_ready = bool(
            complete
            and snapshot.collateral_balance is not None
            and snapshot.collateral_balance >= self.config.risk.maximum_total_debit
        )
        self.capabilities.entry_allowance_ready = bool(
            complete
            and snapshot.collateral_allowance is not None
            and snapshot.collateral_allowance >= self.config.risk.maximum_total_debit
        )
        prior_exposure_blockers = tuple(
            blocker
            for blocker in snapshot.blockers
            if blocker.startswith("PRIOR_POSITION") or blocker.startswith("PRIOR_OPEN_ORDER")
        )
        self.capabilities.prior_scope_clear = complete and not prior_exposure_blockers
        for code in tuple(self.capabilities.extra_blockers):
            if code.startswith("ACCOUNT_STATE_"):
                self.capabilities.clear_blocker(code)
        for index, blocker in enumerate(snapshot.blockers):
            self.capabilities.set_blocker(f"ACCOUNT_STATE_{index}", blocker)
        if snapshot.status in {AccountSnapshotStatus.UNAVAILABLE, AccountSnapshotStatus.STALE}:
            self.capabilities.set_blocker("ACCOUNT_STATE_AUTHORITY", snapshot.status.value)
        else:
            self.capabilities.clear_blocker("ACCOUNT_STATE_AUTHORITY")
        self._record_readiness_if_changed()

    async def _bind_pending_market(self) -> None:
        session = self.pending_market
        if session is None or session is self.active_market:
            return
        state = self.lifecycle.state
        if state is not None and not state.terminal:
            return
        if state is not None:
            self.lifecycle.release_terminal()
            self.selected_instrument = None
            self.selected_token_id = None
        previous_market_id = None
        if self.active_market is not None:
            previous_market = self.active_market.market
            previous_market_id = previous_market.condition_id or previous_market.market_id.value
        self.pending_market = None
        self.capabilities.active_market_ready = False
        self.capabilities.account_reads_ready = False
        self.capabilities.user_stream_ready = False
        self.capabilities.prior_scope_clear = False
        self.capabilities.entry_allowance_ready = False
        self.capabilities.collateral_ready = False
        await self.gateway.stop_user_stream()
        market = session.market
        market_id = market.condition_id or market.market_id.value
        token_ids = (market.yes.token_id.value, market.no.token_id.value)
        account_state = self.account_state_authority.current(market_id)
        self.active_market = session
        self.capabilities.active_market_ready = True
        self._apply_account_snapshot(account_state)
        matching_restored = [
            restored
            for restored in self.restored_sessions
            if restored.identity is not None
            and restored.identity.market_id in {market.condition_id, market.market_id.value}
            and restored.identity.token_id in token_ids
        ]
        foreign_restored = [
            restored for restored in self.restored_sessions if restored not in matching_restored
        ]
        if foreign_restored:
            self.capabilities.set_blocker(
                "UNRESOLVED_PRIOR_MARKET_SESSION",
                ",".join(str(restored.session_id) for restored in foreign_restored),
            )
        if len(matching_restored) > 1:
            self.capabilities.set_blocker("MULTIPLE_RESTORED_SESSIONS")
        restored = matching_restored[0] if len(matching_restored) == 1 else None
        self.stream_session_id = (
            str(restored.session_id)
            if restored is not None
            else f"{self.config.run_name}-{uuid4()}"
        )
        await self.gateway.start_user_stream(
            session_id=self.stream_session_id,
            markets=(market.condition_id or market.market_id.value,),
            on_evidence=self.lifecycle.on_stream_evidence,
            on_gap=self.lifecycle.on_stream_gap,
            on_ready=self.lifecycle.on_stream_ready,
        )
        self.run_recorder.record(
            "MARKET_BOUND",
            {
                "market_id": market.market_id.value,
                "condition_id": market.condition_id,
                "window_id": session.window_id,
                "entry_enabled": session.entry_enabled,
                "stream_session_id": self.stream_session_id,
                "account_state_status": account_state.status.value,
                "account_state_observed_at": account_state.observed_at,
                "account_state_blockers": list(account_state.blockers),
                "restored_session_count": len(matching_restored),
            },
        )
        self._record_readiness_if_changed()
        if previous_market_id is not None and previous_market_id != market_id:
            self.account_state_authority.retire(previous_market_id)
        if restored is not None:
            self.lifecycle.resume(self.stream_session_id)
            assert restored.identity is not None
            self.selected_token_id = restored.identity.token_id
            self.selected_instrument = next(
                (
                    instrument
                    for instrument in (market.yes, market.no)
                    if instrument.token_id.value == self.selected_token_id
                ),
                None,
            )
            reconciled = await self.lifecycle.reconcile()
            if reconciled is not None and reconciled.terminal:
                self.restored_sessions.remove(restored)
            if reconciled is None or not reconciled.reconciliation_complete:
                self.capabilities.set_blocker("PRIOR_SESSION_RECONCILIATION_INCOMPLETE")
            elif reconciled.has_exposure and self.selected_instrument is not None:
                self.capabilities.selected_token_sellable = reconciled.sellable_shares > 0
                self._enqueue_intent(
                    FlattenIntent(
                        intent_id=new_intent_id(),
                        strategy_id=ready_binding_strategy_id_from_session(restored),
                        instrument_id=self.selected_instrument.instrument_id,
                        market_id=market.market_id,
                        created_at=datetime.now(timezone.utc),
                        correlation_id=ready_fallback_correlation(),
                        causation_id=None,
                        reason_code="CRASH_RECOVERY_FLATTEN",
                    )
                )
            elif reconciled.has_unresolved_mutation_attempt:
                await self.lifecycle.mark_manual("ambiguous_prior_submission")
                self.capabilities.set_blocker("AMBIGUOUS_PRIOR_SUBMISSION")
                self.stop_requested = True
            else:
                self.stop_requested = bool(reconciled is not None and reconciled.terminal)

    def _position_cost(self, state: ExecutionSessionState | None) -> Decimal:
        if state is None:
            return Decimal("0")
        return sum(
            (trade.shares * trade.price for trade in state.trades.values() if trade.side == "BUY"),
            Decimal("0"),
        )

    def on_evaluation(self, market_runtime: ZGapMarketRuntime) -> list[dict[str, Any]]:
        ready = market_runtime.prepare_aligned_eval()
        if not ready.ok or ready.snapshot is None or ready.binding is None:
            self.capabilities.model_ready = False
            self.run_recorder.record(
                "EVALUATION_SKIPPED",
                {"reasons": list(ready.skip_reasons)},
            )
            self._record_readiness_if_changed()
            return [{"kind": "skipped", "reasons": list(ready.skip_reasons)}]
        state = self.lifecycle.state
        exposed = bool(state is not None and state.has_exposure)
        market = ready.session.market if ready.session is not None else ready.snapshot.market
        instrument = self.selected_instrument
        life = _lifecycle_view(
            state, instrument_id=None if instrument is None else instrument.instrument_id
        )
        caps = self.capabilities.snapshot(exposed=exposed)
        context = DecisionContext(
            run_id=RunId(self.config.run_name),
            snapshot=ready.snapshot,
            target_notional=self.config.risk.target_notional,
            lifecycle=life,
            position_quantity=Decimal("0") if state is None else state.confirmed_position_shares,
            position_cost_total=self._position_cost(state),
            now=ready.snapshot.observed_at,
            entry_allowed=caps.entry_executable and not exposed and state is None,
            entry_block_reason=None if caps.entry_executable else ",".join(caps.blockers),
            exit_allowed=caps.exit_executable,
            exit_block_reason=None if caps.exit_executable else ",".join(caps.blockers),
            unknown_inventory=not self.capabilities.prior_scope_clear,
            kill_switch_active=self.capabilities.kill_switch_active,
        )
        result = ready.binding.evaluate(
            market_snapshot=ready.snapshot,
            causation_id=ready.snapshot.causation_id,
            correlation_id=ready.snapshot.correlation_id,
            trigger="feed",
            context=context,
            settlement_reference=(ready.dyn.chainlink_raw if ready.dyn is not None else None),
            settlement_reference_fresh=(
                ready.dyn is not None and ready.dyn.chainlink_raw is not None
            ),
        )
        decision_input = result.reporting_context.get("decision_input")
        self.capabilities.model_ready = bool(
            decision_input is not None and decision_input.model.ready
        )
        decision_evidence = dict(result.decision.evidence)
        readiness_evidence = decision_evidence.get("readiness")
        if not isinstance(readiness_evidence, dict):
            readiness_evidence = {}
        raw_blockers = decision_evidence.get("blockers", ())
        if not isinstance(raw_blockers, (list, tuple)):
            raw_blockers = ()
        strategy_blockers = tuple(str(value) for value in raw_blockers)
        strategy_inputs_eligible = bool(
            decision_evidence.get(
                "strategy_inputs_eligible",
                readiness_evidence.get("strategy_inputs_eligible", False),
            )
        )
        decision_record = {
            "decided_at": result.decision.decided_at.isoformat(),
            "action": result.decision.action.value,
            "reason_code": result.decision.reason_code,
            "market_id": market.market_id.value,
            "window_id": ready.session.window_id if ready.session is not None else None,
            "intent_count": len(result.intents),
            "capabilities": self.capabilities.snapshot(exposed=exposed).__dict__,
            "strategy_inputs_eligible": strategy_inputs_eligible,
            "strategy_blockers": list(strategy_blockers),
            "decision_evidence": decision_evidence,
            "calibration": dict(result.reporting_context.get("calibration") or {}),
        }
        self.decisions.append(decision_record)
        self.run_recorder.record("STRATEGY_DECISION", decision_record)
        self._record_readiness_if_changed()
        for intent in result.intents:
            key = intent.semantic_key()
            if key not in self.seen_intents:
                self.seen_intents.add(key)
                self._enqueue_intent(intent)
                self.run_recorder.record(
                    "INTENT_EMITTED",
                    {
                        "intent_id": intent.intent_id.value,
                        "intent_type": type(intent).__name__,
                        "reason_code": intent.reason_code,
                        "market_id": intent.market_id.value,
                        "instrument_id": intent.instrument_id.value,
                        "created_at": intent.created_at,
                    },
                )
        return [{"kind": "evaluated", **decision_record}]

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

    async def _consume_intent(self) -> None:
        if self.intent_queue.empty():
            return
        intent = self.intent_queue.get_nowait()
        candidate_monotonic_ns = self.intent_candidate_monotonic_ns.get(intent.intent_id.value)
        try:
            if isinstance(intent, EnterIntent):
                token_id, instrument = self._select_token(intent)
                if self.active_market is None or self.stream_session_id is None:
                    raise RuntimeError("active execution binding is not prepared")
                market = self.active_market.market
                market_id = market.condition_id or market.market_id.value
                account_state = self.account_state_authority.current(market_id)
                self._apply_account_snapshot(account_state)
                if account_state.status is not AccountSnapshotStatus.READY:
                    self.run_recorder.record(
                        "INTENT_BLOCKED",
                        {
                            "intent_id": intent.intent_id.value,
                            "intent_type": type(intent).__name__,
                            "reason": "ACCOUNT_STATE_NOT_AUTHORITATIVE",
                            "account_state_status": account_state.status.value,
                            "account_state_blockers": list(account_state.blockers),
                        },
                    )
                    return
                baseline = account_state.token(token_id)
                self.account_state = account_state
                self.selected_token_id = token_id
                self.selected_instrument = instrument
                await self.lifecycle.open(
                    session_id=self.stream_session_id,
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
                if state is not None:
                    self.capabilities.selected_token_sellable = state.sellable_shares > 0
                    if state.terminal:
                        self.stop_requested = True
            elif isinstance(intent, (ExitIntent, FlattenIntent)):
                if self.selected_token_id is None:
                    raise RuntimeError("exit intent has no selected token")
                state = await self.lifecycle.submit_exit(
                    intent,
                    token_id=self.selected_token_id,
                    protective=isinstance(intent, FlattenIntent),
                    candidate_monotonic_ns=candidate_monotonic_ns,
                )
                self.capabilities.selected_token_sellable = state.sellable_shares > 0
                if state.terminal:
                    self.stop_requested = True
            else:
                raise TypeError(f"unsupported strategy intent: {type(intent).__name__}")
        except Exception as exc:  # noqa: BLE001 - persisted report owns run failure
            self._record_runtime_error("intent", exc)
        finally:
            self.intent_candidate_monotonic_ns.pop(intent.intent_id.value, None)
            self.intent_queue.task_done()

    async def on_async_tick(self, market_runtime: ZGapMarketRuntime) -> None:
        await self._bind_pending_market()
        if self.active_market is not None:
            market = self.active_market.market
            market_id = market.condition_id or market.market_id.value
            self._apply_account_snapshot(self.account_state_authority.current(market_id))
        ready = self._market_data_ready()
        if self.active_market is not None and self.active_market.market.event_end is not None:
            tau = (
                self.active_market.market.event_end
                - market_runtime.zgap.time_authority.now_corrected_utc()
            ).total_seconds()
            self.capabilities.entry_window_open = (
                self.active_market.entry_enabled
                and self.config.strategy.entry.tau_min_s
                <= tau
                <= self.config.strategy.entry.tau_max_s
            )
            state = self.lifecycle.state
            if (
                state is not None
                and state.has_exposure
                and tau <= self.config.lifecycle.manual_deadline_before_end_s
            ):
                await self.lifecycle.mark_manual("manual_deadline_reached_with_exposure")
                self.stop_requested = True
                return
            if (
                state is not None
                and state.has_exposure
                and tau <= self.config.lifecycle.mandatory_exit_before_end_s
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
        # Entry intents are only produced by a ready evaluation. Exit and crash-
        # recovery intents must still run while public data is temporarily
        # degraded; their final gate captures and validates a fresh BookView on
        # every submission retry.
        if ready or not self.intent_queue.empty():
            await self._consume_intent()
        state = self.lifecycle.state
        if state is not None and state.has_exposure:
            reconciled = await self.lifecycle.reconcile()
            if reconciled is not None:
                self.capabilities.selected_token_sellable = reconciled.sellable_shares > 0

    def readiness_dict(self) -> dict[str, Any]:
        state = self.lifecycle.state
        snap = self.capabilities.snapshot(exposed=bool(state and state.has_exposure))
        return {
            "blockers": [{"code": value} for value in snap.blockers],
            **snap.__dict__,
        }

    async def run(self) -> TradingRunResult:
        runtime = ZGapMarketRuntime.create(
            clock=SystemClock(),
            zgap_config=self.config.strategy,
            require_ssr_price_match=self.config.market.require_ssr_price_match,
            target_notional=self.config.risk.target_notional,
        )
        fatal_exception: BaseException | None = None
        fatal_error: dict[str, Any] | None = None
        reporting_failures: list[str] = []
        run_evidence: tuple[RunEvidenceRecord, ...] = ()
        try:
            self.market_summary = await run_market_data_runtime(
                out_dir=self.output_directory,
                run_id=self.config.run_name,
                min_seals=999,
                max_duration_s=self.config.market.maximum_duration_s,
                duration_anchor="prepared_window_start",
                preparation_timeout_s=self.config.market.preparation_lead_s + 300,
                prep_lead_s=self.config.market.preparation_lead_s,
                binance_symbol=self.config.market.binance_symbol,
                evaluation_interval_s=self.config.market.evaluation_interval_s,
                on_evaluation=self.on_evaluation,
                on_active_session=self.on_active_market,
                on_prepared_session=self.on_prepared_market,
                should_stop=lambda: self.stop_requested,
                stop_when_seals_met=False,
                runtime=runtime,
                require_ssr_price_match=self.config.market.require_ssr_price_match,
                zgap_config=self.config.strategy,
                target_notional=self.config.risk.target_notional,
                before_promote=self.can_promote,
                on_feed_supervisor=self.on_feed_supervisor,
                on_async_tick=self.on_async_tick,
                evaluation_ready=self._entry_evaluation_ready,
                readiness_diagnostics=self.readiness_dict,
                on_runtime_error=self._record_runtime_error,
                max_clock_uncertainty_ms=(
                    self.config.strategy.ptb_time_quality.max_clock_uncertainty_ms
                ),
            )
        except BaseException as exc:  # always project a report before propagating interrupts
            fatal_exception = exc
            fatal_error = {
                "stage": "market_data_runtime",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            self._record_runtime_error("market_data_runtime", exc)

        state = self.lifecycle.state
        try:
            mutation_attempts = (
                0 if state is None else self.coordinator.mutation_attempt_count(state.session_id)
            )
        except Exception as exc:
            mutation_attempts = None
            reporting_failures.append(f"mutation_projection:{type(exc).__name__}:{exc}")
        try:
            self.execution_timeline = self._build_execution_timeline(state)
        except Exception as exc:  # execution projection failure is report degradation
            self.execution_timeline = []
            reporting_failures.append(f"timeline_projection:{type(exc).__name__}:{exc}")

        try:
            await self.account_state_authority.close()
        except Exception as exc:
            self._record_runtime_error("account_state_shutdown", exc)
            if fatal_error is None:
                fatal_error = {
                    "stage": "account_state_shutdown",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }

        try:
            await self.coordinator.close()
        except Exception as exc:  # the report must record shutdown failure
            self._record_runtime_error("execution_shutdown", exc)
            if fatal_error is None:
                fatal_error = {
                    "stage": "execution_shutdown",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }

        self.run_recorder.record(
            "RUN_FINISHED",
            {
                "execution_phase": None if state is None else state.phase.value,
                "mutation_attempts": mutation_attempts,
                "fatal": fatal_error is not None,
            },
        )
        try:
            run_evidence = await self.run_recorder.snapshot_and_close()
        except Exception as exc:
            reporting_failures.append(f"run_evidence_snapshot:{type(exc).__name__}:{exc}")
            try:
                self.run_recorder.journal.close()
            except Exception as close_exc:
                reporting_failures.append(
                    f"run_evidence_close:{type(close_exc).__name__}:{close_exc}"
                )
        reporting_failures.extend(self.run_recorder.failures)

        result = self._write_report(
            state=state,
            mutation_attempts=mutation_attempts,
            run_evidence=run_evidence,
            fatal_error=fatal_error,
            reporting_failures=reporting_failures,
        )
        if isinstance(fatal_exception, (KeyboardInterrupt, asyncio.CancelledError)):
            raise fatal_exception
        return result

    def _build_execution_timeline(
        self, state: ExecutionSessionState | None
    ) -> list[dict[str, Any]]:
        if state is None or state.session_id is None:
            return []
        rows: dict[str, dict[str, Any]] = {}
        for order_id, order in state.orders.items():
            candidate_raw = order.spec.metadata.get("candidate_at")
            candidate_mono_raw = order.spec.metadata.get("candidate_monotonic_ns")
            candidate = None
            if candidate_raw:
                try:
                    candidate = datetime.fromisoformat(str(candidate_raw).replace("Z", "+00:00"))
                except ValueError:
                    candidate = None
            rows[order_id] = {
                "order_id": order_id,
                "role": order.role.value,
                "candidate_at": None if candidate is None else candidate.isoformat(),
                "requested_at": None,
                "prepared_at": None,
                "pre_dispatch_failed_at": None,
                "dispatch_authorized_at": None,
                "http_post_started_at": None,
                "http_post_completed_at": None,
                "submission_outcome": None,
                "pre_dispatch_failure": None,
                "requested_protection_price": (
                    None
                    if order.requested_protection_price is None
                    else str(order.requested_protection_price)
                ),
                "effective_protection_price": (
                    None
                    if order.effective_protection_price is None
                    else str(order.effective_protection_price)
                ),
                "tick_size": None if order.tick_size is None else str(order.tick_size),
                "_candidate_mono": (
                    None if candidate_mono_raw is None else int(candidate_mono_raw)
                ),
            }
        for event in self.coordinator.journal.load(state.session_id):
            if isinstance(
                event,
                (
                    OrderRequested,
                    OrderPrepared,
                    OrderPreDispatchFailed,
                    DispatchAuthorized,
                    SubmissionAttempted,
                    SubmissionResponseObserved,
                    SubmissionFailed,
                ),
            ):
                event_order_id = (
                    event.order.order_id if isinstance(event, OrderRequested) else event.order_id
                )
                row = rows.get(event_order_id)
                if row is None:
                    continue
                if isinstance(event, OrderRequested):
                    row["requested_at"] = event.observed_at.isoformat()
                    row["_requested_mono"] = event.observed_monotonic_ns
                elif isinstance(event, OrderPrepared):
                    row["prepared_at"] = event.observed_at.isoformat()
                    row["_prepared_mono"] = event.observed_monotonic_ns
                elif isinstance(event, OrderPreDispatchFailed):
                    row["pre_dispatch_failed_at"] = event.observed_at.isoformat()
                    row["_pre_dispatch_failed_mono"] = event.observed_monotonic_ns
                    row["submission_outcome"] = "pre_dispatch_failed"
                    row["pre_dispatch_failure"] = {
                        "stage": event.stage.value,
                        "error_class": event.error_class,
                        "error_code": event.error_code,
                        "message": event.message,
                    }
                elif isinstance(event, DispatchAuthorized):
                    row["dispatch_authorized_at"] = event.observed_at.isoformat()
                    row["_authorized_mono"] = event.observed_monotonic_ns
                elif isinstance(event, SubmissionAttempted):
                    row["http_post_started_at"] = event.observed_at.isoformat()
                    row["_post_started_mono"] = event.observed_monotonic_ns
                else:
                    row["http_post_completed_at"] = event.observed_at.isoformat()
                    row["_post_completed_mono"] = event.observed_monotonic_ns
                    row["submission_outcome"] = (
                        "response"
                        if isinstance(event, SubmissionResponseObserved)
                        else "ambiguous_failure"
                    )
        result: list[dict[str, Any]] = []
        for row in rows.values():
            pairs = {
                "candidate_to_request_ms": ("_candidate_mono", "_requested_mono"),
                "request_to_prepared_ms": ("_requested_mono", "_prepared_mono"),
                "request_to_pre_dispatch_failure_ms": (
                    "_requested_mono",
                    "_pre_dispatch_failed_mono",
                ),
                "request_to_authorized_ms": ("_requested_mono", "_authorized_mono"),
                "authorized_to_post_ms": ("_authorized_mono", "_post_started_mono"),
                "post_round_trip_ms": ("_post_started_mono", "_post_completed_mono"),
                "candidate_to_post_ms": ("_candidate_mono", "_post_started_mono"),
                "candidate_to_pre_dispatch_failure_ms": (
                    "_candidate_mono",
                    "_pre_dispatch_failed_mono",
                ),
            }
            for label, (start_key, end_key) in pairs.items():
                start, end = row.get(start_key), row.get(end_key)
                row[label] = (
                    None
                    if start is None or end is None
                    else round(max(0, end - start) / 1_000_000, 3)
                )
            result.append({key: value for key, value in row.items() if not key.startswith("_")})
        return result

    def _write_report(
        self,
        *,
        state: ExecutionSessionState | None,
        mutation_attempts: int | None,
        run_evidence: tuple[RunEvidenceRecord, ...],
        fatal_error: dict[str, Any] | None,
        reporting_failures: list[str],
    ) -> TradingRunResult:
        path = self.output_directory / "run_summary.json"
        try:
            capabilities = self.readiness_dict()
        except Exception as exc:
            capabilities = {}
            reporting_failures.append(f"capability_projection:{type(exc).__name__}:{exc}")
        try:
            market_data = None if self.market_summary is None else self.market_summary.to_dict()
        except Exception as exc:
            market_data = None
            reporting_failures.append(f"market_projection:{type(exc).__name__}:{exc}")
        source = RunReportInput(
            run_instance_id=self.run_instance_id,
            configured_run_name=self.config.run_name,
            live_requested=True,
            state=state,
            mutation_attempts=mutation_attempts,
            execution_timeline=self.execution_timeline,
            capabilities=capabilities,
            market_data=market_data,
            run_evidence=run_evidence,
            runtime_errors=self.runtime_errors,
            fatal_error=fatal_error,
            reporting_failures=reporting_failures,
        )
        try:
            payload = build_run_report(source)
            write_run_report(path, payload)
        except Exception as exc:  # primitive-only final fallback
            payload = write_emergency_report(
                path,
                run_instance_id=self.run_instance_id,
                configured_run_name=self.config.run_name,
                live_requested=True,
                mutation_attempts=mutation_attempts,
                fatal_error=fatal_error,
                reporting_error=exc,
            )
        return TradingRunResult(
            outcome=str(payload["outcome"]),
            ok=bool(payload["ok"]),
            report_path=path,
            mutation_attempts=mutation_attempts,
            state=state,
        )


def ready_binding_strategy_id(runtime: ZGapMarketRuntime):
    return runtime.zgap.strategy_id


def ready_binding_strategy_id_from_session(state: ExecutionSessionState):
    from tyrex_pm.core.ids import StrategyId

    if state.identity is None:
        raise RuntimeError("restored session has no identity")
    return StrategyId(state.identity.strategy_id)


def ready_fallback_correlation():
    from tyrex_pm.core.ids import new_correlation_id

    return new_correlation_id()


async def run_trading_runtime(
    *,
    config: TradingRunConfig,
    output_directory: Path,
    dotenv: Path | None,
) -> TradingRunResult:
    run_instance_id = str(uuid4())
    try:
        runtime = await TradingRuntime.create(
            config=config,
            output_directory=output_directory,
            env=load_dotenv_values(dotenv),
            run_instance_id=run_instance_id,
        )
    except Exception as exc:
        path = output_directory / "run_summary.json"
        fatal_error = {
            "stage": "runtime_creation",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        payload = write_emergency_report(
            path,
            run_instance_id=run_instance_id,
            configured_run_name=config.run_name,
            live_requested=True,
            mutation_attempts=0,
            fatal_error=fatal_error,
            reporting_error=exc,
        )
        return TradingRunResult(
            outcome=str(payload["outcome"]),
            ok=False,
            report_path=path,
            mutation_attempts=0,
            state=None,
        )
    return await runtime.run()
