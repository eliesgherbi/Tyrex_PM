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
from tyrex_pm.core.ids import InstrumentId, RunId
from tyrex_pm.core.intents import (
    CancelIntent,
    EnterIntent,
    ExitIntent,
    FlattenIntent,
    HoldToResolutionIntent,
    LiquidityRole,
    new_intent_id,
)
from tyrex_pm.core.time_authority import SnapshotTimeAuthority, TimeAuthority
from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
    AccountStateAuthority,
    AccountStatePolicy,
    AccountStateSnapshot,
)
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator, DispatchBlocked
from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    ExecutionRole,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
)
from tyrex_pm.execution.exit_arbitration import admit_exit
from tyrex_pm.execution.exit_sizing import (
    can_post_exit,
    is_exempt_prior_position_blocker,
    remaining_exit_shares,
    venue_legal_sell_shares,
)
from tyrex_pm.execution.final_gate import FinalExecutionGate, FinalGatePolicy
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.orders import TimeInForce
from tyrex_pm.execution.outcome_reasons import classify_reason, classify_reasons
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import (
    ExecutionPhase,
    ExecutionSessionState,
    KnownDustResidual,
)
from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.market_data.book_health import ConnectionHealth, FeedSyncPhase, SyncHealth
from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal
from tyrex_pm.persistence.run_evidence_journal import (
    RunEvidenceRecord,
    RunEvidenceRecorder,
    SqliteRunEvidenceJournal,
)
from tyrex_pm.protection import (
    ArmedProtection,
    ProtectionPhase,
    ProtectionRegistry,
    build_protection_intent,
    mark_triggered,
    observe_mark,
)
from tyrex_pm.reporting.run_report import (
    RunReportInput,
    build_run_report,
    write_emergency_report,
    write_run_report,
)
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.market_data_runtime import MarketDataSummary, run_market_data_runtime
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime, ZGapMarketRuntime
from tyrex_pm.runtime.run_config import (
    TradingRunConfig,
    effective_entry_cutoff_s,
    entry_tau_bounds,
    max_clock_uncertainty_ms,
    strategy_entry_cutoff_s,
)
from tyrex_pm.runtime.settlement_hooks import SettlementHooks
from tyrex_pm.strategies.context import (
    DecisionContext,
    ReferenceFacts,
    StrategyLifecycleSnapshot,
    StrategyPositionPhase,
)
from tyrex_pm.strategies.outcomes import (
    EntryOutcome,
    EntryOutcomeKind,
    ExitOutcome,
    ExitOutcomeKind,
    RetryClass,
)

# Late-evidence watch of released entry attempts in the current window [s].
LATE_EVIDENCE_WATCH_S = 60.0
LATE_EVIDENCE_POLL_S = 5.0

# Soft rewarm before official SDK AsyncOrderMetadataCache TTL (10 minutes).
_ORDER_METADATA_WARM_TTL_S = 8 * 60
# Refresh order metadata this long before it expires, also while its market is active.
_ORDER_METADATA_REFRESH_LEAD_S = 90.0
_ORDER_METADATA_REFRESH_MAX_ATTEMPTS = 5
_ORDER_METADATA_RETRY_BASE_S = 2.0
_ORDER_METADATA_RETRY_MAX_S = 30.0
_ORDER_METADATA_WARM_MAX_PRICE = Decimal("0.99")
_ORDER_METADATA_WARM_AMOUNT = Decimal("1")


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


_PREPARE_TIMING_FIELDS = (
    "prepare_queue_wait_ms",
    "tick_resolve_ms",
    "create_market_order_ms",
    "refresh_wait_ms",
    "refresh_elapsed_ms",
)


def _try_execution_epoch(coordinator: object):
    from tyrex_pm.execution.inspection_sync import capture_execution_epoch

    try:
        return capture_execution_epoch(coordinator)
    except Exception:
        return None


def _merge_prepare_timings(row: dict[str, Any], event: Any) -> None:
    """Copy a measured field. ``None`` leaves the earlier measurement in place."""
    for name in _PREPARE_TIMING_FIELDS:
        incoming = getattr(event, name, None)
        if incoming is not None:
            row[name] = incoming
    miss = getattr(event, "order_metadata_cache_miss", None)
    if miss is not None:
        row["order_metadata_cache_miss"] = bool(miss)


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
        ExecutionPhase.COMPLETED_FLAT_WITH_DUST: StrategyPositionPhase.FLAT,
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


def _hold_committed(runtime: Any) -> bool:
    """True only for a hold-profile run whose active window's holding is committed."""
    hooks = getattr(runtime, "settlement_hooks", None)
    session = getattr(runtime, "active_market", None)
    if hooks is None or session is None:
        return False
    return hooks.is_committed(session.window_id)


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
    intent_wake: asyncio.Event = field(default_factory=asyncio.Event)
    seen_intents: set[str] = field(default_factory=set)
    intent_candidate_monotonic_ns: dict[str, int] = field(default_factory=dict)
    stop_requested: bool = False
    restored_sessions: list[ExecutionSessionState] = field(default_factory=list)
    execution_timeline: list[dict[str, Any]] = field(default_factory=list)
    _last_readiness_key: tuple[Any, ...] | None = None
    protection_registry: ProtectionRegistry = field(default_factory=ProtectionRegistry)
    known_dust_residuals: list[KnownDustResidual] = field(default_factory=list)
    _order_metadata_warmed_at: dict[str, float] = field(default_factory=dict)
    _order_metadata_warm_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    # Single flight per token + bounded retries with backoff for metadata refresh.
    _order_metadata_inflight: set[str] = field(default_factory=set)
    _last_recheck_failure_key: tuple[Any, ...] | None = None
    _order_metadata_failures: dict[str, int] = field(default_factory=dict)
    _order_metadata_retry_after: dict[str, float] = field(default_factory=dict)
    _intent_dispatcher_task: asyncio.Task[None] | None = None
    _intent_dispatcher_stop: asyncio.Event = field(default_factory=asyncio.Event)
    _cleanup_done: bool = False
    _cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    time_authority: TimeAuthority | None = None
    indicator_graph: Any | None = None
    _required_feature_specs: tuple[Any, ...] = ()
    prior_session_risk: bool = False
    reproducibility: dict[str, Any] | None = None
    startup_outcome: str | None = None
    startup_report: dict[str, Any] | None = None
    recovery_sessions: list[dict[str, Any]] = field(default_factory=list)
    _state_lock: Any = None
    _recovery_binding_id: str | None = None
    _recovery_book: Any = None
    _assessment_valid_until: datetime | None = None
    _assessment_generation: str | None = None
    _startup_invalidated_reason: str | None = None
    _startup_monitor: asyncio.Task[None] | None = None
    _strategy_started: bool = False
    _process_mutation_attempts: int | None = None
    decision_reader: Any = None
    _startup_wallet: str | None = None
    _last_inspection_publication: str = "discarded"
    # Present only for settlement lifecycles (see runtime/settlement_hooks.py).
    settlement_hooks: Any | None = None
    _market_runtime_ref: Any = None
    # Continuous operation: resolution worker, per-window summaries, stop reason.
    settlement_worker: Any | None = None
    window_summaries: list[dict[str, Any]] = field(default_factory=list)
    run_stop_reason: str | None = None
    _last_exit_reason: dict[str, str] = field(default_factory=dict)
    _last_release_at: datetime | None = None
    _window_started_at: dict[str, str] = field(default_factory=dict)
    # Window whose committed hold still waits for trade confirmation before detaching.
    _pending_detach: str | None = None
    # Read-only Polygon RPC for payout tracking, from the process env or the run's .env.
    polygon_rpc_url: str | None = None
    # Decision/explain recording (recording.host); None at level ``essential``.
    decision_recording: Any | None = None
    recording_status: dict[str, Any] | None = None
    _recording_status_mono: float = 0.0
    _recording_failed_reported: bool = False
    _raw_fault_handled: bool = False

    @classmethod
    async def create(
        cls,
        *,
        config: TradingRunConfig,
        output_directory: Path,
        env: dict[str, str],
        gateway: PolymarketAsyncGateway | None = None,
        run_instance_id: str | None = None,
        live_requested: bool = True,
    ) -> "TradingRuntime":
        output_directory.mkdir(parents=True, exist_ok=True)
        from tyrex_pm.execution.process_lock import StateDirectoryLock

        state_lock = StateDirectoryLock(config.state_directory)
        state_lock.acquire()
        try:
            return await cls._create_locked(
                config=config,
                output_directory=output_directory,
                env=env,
                gateway=gateway,
                run_instance_id=run_instance_id,
                live_requested=live_requested,
                state_lock=state_lock,
            )
        except BaseException:
            state_lock.release()
            raise

    @classmethod
    async def _create_locked(
        cls,
        *,
        config: TradingRunConfig,
        output_directory: Path,
        env: dict[str, str],
        gateway: PolymarketAsyncGateway | None,
        run_instance_id: str | None,
        live_requested: bool,
        state_lock: Any,
    ) -> "TradingRuntime":
        active_gateway = gateway or await PolymarketAsyncGateway.from_env(
            env, isolated_reads=config.account.isolated_read_clients
        )
        reads_isolated = bool(getattr(active_gateway, "reads_isolated", False))
        account_reads = (
            active_gateway.account_reads() if reads_isolated else active_gateway
        )
        reconciliation_reads = (
            active_gateway.reconciliation_reads() if reads_isolated else active_gateway
        )
        capabilities = CapabilityController(
            live_requested=live_requested,
            kill_switch_active=config.risk.kill_switch_active,
        )
        holder: dict[str, Any] = {"runtime": None}

        def capture_book():
            runtime = holder["runtime"]
            if runtime is not None and runtime._recovery_book is not None:
                return runtime._recovery_book()
            if runtime is None or runtime.feed_supervisor is None:
                return None
            return runtime.feed_supervisor.active_view()

        def binding_id() -> str | None:
            runtime = holder["runtime"]
            if runtime is not None and runtime._recovery_binding_id is not None:
                return runtime._recovery_binding_id
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

        time_authority = SnapshotTimeAuthority(
            clock=SystemClock(),
            max_uncertainty_ms=max_clock_uncertainty_ms(config),
        )
        from tyrex_pm.strategies.registry import get_strategy_plugin, plugin_indicator_specs

        plugin = get_strategy_plugin(config.strategy_kind)
        from tyrex_pm.indicators.readiness import features_ready_reason, required_feature_specs

        required_features = required_feature_specs(
            plugin_indicator_specs(plugin, config.strategy)
        )

        def features_ready() -> bool | str:
            runtime = holder["runtime"]
            if runtime is None:
                return "FEATURE_NOT_READY"
            # Shared with replay contract v2 (indicators.readiness).
            return features_ready_reason(
                runtime.indicator_graph, runtime._required_feature_specs
            )

        final_gate = FinalExecutionGate(
            capture_book=capture_book,
            capabilities=capabilities,
            active_binding_id=binding_id,
            policy=FinalGatePolicy(
                max_book_age_ms=config.market.maximum_book_age_ms,
                max_candidate_age_ms=config.market.maximum_candidate_age_ms,
            ),
        )
        final_gate.features_ready = features_ready
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
            if not restored.safe_to_open_entry:
                restored_sessions.append(restored)
        lifecycle = ExecutionLifecycle(
            coordinator=coordinator,
            reconciler=SessionReconciler(reconciliation_reads),
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
                sell_size_increment=config.lifecycle.sell_size_increment,
                dust_abs_shares=config.lifecycle.dust_abs_shares,
                dust_max_notional=config.lifecycle.dust_max_notional,
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
                account_reads,
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
            time_authority=time_authority,
            _required_feature_specs=required_features,
            prior_session_risk=any(
                not restored.safe_to_open_entry for restored in restored_sessions
            ),
        )
        holder["runtime"] = result
        result.settlement_hooks = SettlementHooks.from_config(config)
        result.polygon_rpc_url = env.get("TYREX_POLYGON_RPC_URL") or os.environ.get(
            "TYREX_POLYGON_RPC_URL"
        )
        # Shared client only: signing waits for an in-flight refresh and serializes
        # with account reads. With isolated read clients nothing is shared, so the
        # order path never waits for background account work.
        active_gateway.client_guard = None if reads_isolated else result.account_state_authority
        result.run_recorder.record(
            "ACCOUNT_READ_ISOLATION",
            {"isolated_read_clients": reads_isolated,
             "configured": config.account.isolated_read_clients},
        )
        active_gateway.fill_context = result.lifecycle.fill_context
        final_gate.entry_cutoff_s = effective_entry_cutoff_s(config)
        final_gate.entry_tau_s = result._current_entry_tau_s
        final_gate.startup_buy_block = result.startup_buy_block
        final_gate.note_stream_gap = result.note_startup_stream_gap
        result._state_lock = state_lock
        result.coordinator.recovery_generation = result.run_instance_id
        result.capabilities.set_blocker(
            "STARTUP_CHECK_PENDING",
            "a fresh account check is required before any new BUY",
        )
        active_gateway.prepare_budget_ms = config.lifecycle.prepare_budget_ms
        active_gateway.refresh_wait_budget_ms = config.lifecycle.refresh_wait_budget_ms
        active_gateway.refresh_revalidate_after_ms = config.lifecycle.refresh_revalidate_after_ms
        active_gateway.evidence_sink = run_recorder.record
        for restored in coordinator.states.values():
            result._remember_dust(restored)
        run_recorder.record(
            "RUN_STARTED",
            {
                "configured_run_name": config.run_name,
                "strategy_kind": config.strategy_kind,
                "output_directory": str(output_directory),
                "live_requested": live_requested,
                "venue": getattr(config, "venue", "live"),
                "run_mode": getattr(config.market, "run_mode", "single_window"),
                "lifecycle_mode": getattr(config, "lifecycle_mode", None),
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
        self._schedule_order_metadata_warm(market_session)
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
        self._schedule_order_metadata_warm(market_session)
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

    def _market_token_ids(self, market_session: Any) -> tuple[str, ...]:
        market = market_session.market
        return (market.yes.token_id.value, market.no.token_id.value)

    def _token_metadata_is_warm(self, token_id: str, *, now: float | None = None) -> bool:
        warmed_at = self._order_metadata_warmed_at.get(token_id)
        if warmed_at is None:
            return False
        clock = time.monotonic() if now is None else now
        return (clock - warmed_at) <= _ORDER_METADATA_WARM_TTL_S

    def _refresh_order_metadata_capability(self) -> None:
        if self.active_market is None:
            self.capabilities.order_metadata_ready = False
            self._record_readiness_if_changed()
            return
        now = time.monotonic()
        ready = all(
            self._token_metadata_is_warm(token_id, now=now)
            for token_id in self._market_token_ids(self.active_market)
        )
        self.capabilities.order_metadata_ready = ready
        self._record_readiness_if_changed()

    def _token_metadata_refresh_due(self, token_id: str, now: float) -> bool:
        warmed_at = self._order_metadata_warmed_at.get(token_id)
        if warmed_at is None:
            return True
        return (now - warmed_at) >= _ORDER_METADATA_WARM_TTL_S - _ORDER_METADATA_REFRESH_LEAD_S

    def _schedule_order_metadata_warm(
        self, market_session: Any, *, refresh_ahead: bool = False
    ) -> None:
        """Warm order metadata for a market's tokens.

        Without ``refresh_ahead`` only tokens that are not warm are fetched (market prepare /
        activation). With it, tokens close to expiry are refreshed too, so an active market never
        loses readiness mid-window. At most one request per token is in flight; failures back off
        and stop after a bounded number of attempts (entries then stay blocked).
        """
        token_ids = self._market_token_ids(market_session)
        now = time.monotonic()
        pending = tuple(
            token_id
            for token_id in token_ids
            if token_id not in self._order_metadata_inflight
            and (
                self._token_metadata_refresh_due(token_id, now)
                if refresh_ahead
                else not self._token_metadata_is_warm(token_id, now=now)
            )
            and now >= self._order_metadata_retry_after.get(token_id, 0.0)
            and self._order_metadata_failures.get(token_id, 0)
            < _ORDER_METADATA_REFRESH_MAX_ATTEMPTS
        )
        self._refresh_order_metadata_capability()
        if not pending:
            return
        self._order_metadata_inflight.update(pending)
        if refresh_ahead:
            self.run_recorder.record(
                "ORDER_METADATA_REFRESH_SCHEDULED",
                {
                    "token_ids": list(pending),
                    "reason": [
                        "missing_or_expired"
                        if not self._token_metadata_is_warm(token_id, now=now)
                        else "expiring"
                        for token_id in pending
                    ],
                },
            )

        async def _run() -> None:
            try:
                await self._warm_order_metadata(pending)
            except Exception as exc:  # noqa: BLE001 - readiness owns failure
                self._record_runtime_error("order_metadata_warm", exc)
                for token_id in pending:
                    self._order_metadata_warmed_at.pop(token_id, None)
                    self._note_order_metadata_failure(token_id)
                self._refresh_order_metadata_capability()
            finally:
                self._order_metadata_inflight.difference_update(pending)

        task = asyncio.create_task(_run(), name="order-metadata-warm")
        self._order_metadata_warm_tasks.add(task)
        task.add_done_callback(self._order_metadata_warm_tasks.discard)

    def _note_order_metadata_failure(self, token_id: str) -> None:
        failures = self._order_metadata_failures.get(token_id, 0) + 1
        self._order_metadata_failures[token_id] = failures
        delay = min(
            _ORDER_METADATA_RETRY_MAX_S, _ORDER_METADATA_RETRY_BASE_S * (2 ** (failures - 1))
        )
        self._order_metadata_retry_after[token_id] = time.monotonic() + delay
        if failures == _ORDER_METADATA_REFRESH_MAX_ATTEMPTS:
            self.run_recorder.record(
                "ORDER_METADATA_REFRESH_EXHAUSTED",
                {"token_id": token_id, "attempts": failures},
            )

    def _note_order_metadata_success(self, token_id: str) -> None:
        self._order_metadata_failures.pop(token_id, None)
        self._order_metadata_retry_after.pop(token_id, None)

    def _maybe_refresh_active_order_metadata(self) -> None:
        """Tick hook: keep the active market's metadata fresh before it expires."""
        if self.active_market is not None:
            self._schedule_order_metadata_warm(self.active_market, refresh_ahead=True)
        else:
            self._refresh_order_metadata_capability()

    async def _warm_order_metadata(self, token_ids: tuple[str, ...]) -> None:
        self.run_recorder.record(
            "ORDER_METADATA_WARM_STARTED",
            {"token_ids": list(token_ids)},
        )
        result = await self.gateway.warm_order_metadata(
            token_ids,
            max_price=_ORDER_METADATA_WARM_MAX_PRICE,
            max_spend=self.config.risk.maximum_total_debit,
            amount=min(_ORDER_METADATA_WARM_AMOUNT, self.config.risk.maximum_total_debit),
        )
        now = time.monotonic()
        for item in result.tokens:
            payload = {
                "token_id": item.token_id,
                "ok": item.ok,
                "elapsed_ms": item.elapsed_ms,
                "error": item.error,
            }
            if item.ok:
                self._order_metadata_warmed_at[item.token_id] = now
                self._note_order_metadata_success(item.token_id)
                self.run_recorder.record("ORDER_METADATA_WARM_OK", payload)
            else:
                # A failed refresh keeps entries blocked; it is retried with backoff.
                self._order_metadata_warmed_at.pop(item.token_id, None)
                self._note_order_metadata_failure(item.token_id)
                self.run_recorder.record("ORDER_METADATA_WARM_FAILED", payload)
        self._refresh_order_metadata_capability()
        self.run_recorder.record(
            "ORDER_METADATA_WARM_FINISHED",
            {
                "ok": result.ok,
                "warmed_token_ids": list(result.warmed_token_ids),
                "token_results": [
                    {
                        "token_id": item.token_id,
                        "ok": item.ok,
                        "elapsed_ms": item.elapsed_ms,
                        "error": item.error,
                    }
                    for item in result.tokens
                ],
            },
        )

    def can_promote(self) -> bool:
        state = self.lifecycle.state
        if state is not None and not state.safe_to_open_entry:
            return False
        return not self.lifecycle.unresolved_window_sessions()

    def _current_entry_tau_s(self) -> float | None:
        if self.active_market is None or self.active_market.market.event_end is None:
            return None
        if self.time_authority is None:
            return None
        now = self.time_authority.now_corrected_utc()
        return (self.active_market.market.event_end - now).total_seconds()

    async def _inspect_retained_session(
        self,
        restored: ExecutionSessionState,
        *,
        bound_market_id: str,
    ) -> str:
        identity = restored.identity
        payload = {
            "session_id": restored.session_id,
            "phase": restored.phase.value,
            "confirmed_position_shares": str(restored.confirmed_position_shares),
            "open_venue_order_ids": list(restored.resting_limit_venue_ids),
            "mutation_ambiguous": restored.has_unresolved_mutation_attempt,
            "match_awaiting": restored.entry_match_awaiting_confirmation,
        }
        if identity is None:
            payload["host_action"] = "missing_identity"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "missing_identity"
        payload.update(
            {
                "market_id": identity.market_id,
                "window_id": identity.window_id,
                "token_id": identity.token_id,
            }
        )
        same_market = identity.market_id == bound_market_id
        hooks = getattr(self, "settlement_hooks", None)
        if (
            not same_market
            and self.continuous
            and hooks is not None
            and (identity.market_id, identity.token_id)
            in hooks.committed_holds(include_settled=True)
            and not restored.has_journal_uncertainty
        ):
            payload["host_action"] = "committed_hold_tracked"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "foreign_inventory_absent"
        if same_market:
            host_action = "resume_flatten" if restored.has_exposure else (
                "resume_cancel_then_prove"
                if restored.working_maker_entry or restored.resting_limit_venue_ids
                else "block_manual"
            )
            payload["host_action"] = host_action
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return host_action
        if restored.has_journal_uncertainty:
            payload["host_action"] = "block_foreign"
            payload["block_reason"] = "journal_uncertainty"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "block_foreign"
        snapshot = await self.account_state_authority.observe(
            market_id=identity.market_id,
            token_ids=(identity.token_id,),
            required_collateral=self.config.risk.maximum_total_debit,
        )
        payload["scoped_snapshot_status"] = snapshot.status.value
        payload["scoped_snapshot_market_id"] = snapshot.market_id
        payload["scoped_blockers"] = list(snapshot.blockers)
        if (
            snapshot.status
            in {AccountSnapshotStatus.UNAVAILABLE, AccountSnapshotStatus.STALE}
            or snapshot.market_id != identity.market_id
            or identity.token_id not in snapshot.token_ids
            or not snapshot.read_complete
        ):
            payload["host_action"] = "block_foreign"
            payload["block_reason"] = "snapshot_incomplete"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "block_foreign"
        leftover = []
        for blocker in snapshot.blockers:
            if blocker.startswith("PRIOR_OPEN_ORDER"):
                leftover.append(blocker)
            elif blocker.startswith("PRIOR_POSITION") and not is_exempt_prior_position_blocker(
                blocker, self.known_dust_residuals
            ):
                leftover.append(blocker)
        if leftover:
            payload["host_action"] = "block_foreign"
            payload["block_reason"] = "scoped_leftover"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "block_foreign"
        payload["host_action"] = "foreign_inventory_absent"
        self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
        return "foreign_inventory_absent"

    async def _shutdown_execution(self, reason: str) -> None:
        async with self._cleanup_lock:
            if self._cleanup_done:
                return
            self._cleanup_done = True
            self.stop_requested = True
            self.capabilities.entry_window_open = False
            self.capabilities.set_blocker("STOP_REQUESTED", reason)
            await self._stop_intent_dispatcher(cancel_inflight=False)
            worker = getattr(self, "settlement_worker", None)
            if worker is not None:
                await worker.stop()
            summarize = getattr(self, "_summarize_window", None)
            active = getattr(self, "active_market", None)
            if summarize is not None and active is not None:
                summarize(active.window_id, f"stop:{reason}")
            if _hold_committed(self):
                await self._cancel_for_hold()
                self.run_recorder.record("SHUTDOWN_HOLD_PRESERVED", {"reason": reason})
                return
            residual = getattr(self, "_shutdown_residual_hold", None)
            if residual is not None and await residual(reason):
                return
            await self.lifecycle.drain_open_session(
                reason,
                min_order_size=self._held_min_order_size(),
                last_bid=self._held_best_bid(self.active_market),
            )

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
            if (
                (
                    blocker.startswith("PRIOR_POSITION")
                    and not is_exempt_prior_position_blocker(
                        blocker, self.known_dust_residuals
                    )
                    and not self._balance_explained_by_owned(blocker)
                )
                or blocker.startswith("PRIOR_OPEN_ORDER")
            )
        )
        self.capabilities.prior_scope_clear = complete and not prior_exposure_blockers
        for code in tuple(self.capabilities.extra_blockers):
            if code.startswith("ACCOUNT_STATE_"):
                self.capabilities.clear_blocker(code)
        for index, blocker in enumerate(snapshot.blockers):
            if is_exempt_prior_position_blocker(blocker, self.known_dust_residuals):
                continue
            if self._balance_explained_by_owned(blocker):
                continue
            self.capabilities.set_blocker(f"ACCOUNT_STATE_{index}", blocker)
        if snapshot.status in {AccountSnapshotStatus.UNAVAILABLE, AccountSnapshotStatus.STALE}:
            self.capabilities.set_blocker("ACCOUNT_STATE_AUTHORITY", snapshot.status.value)
        else:
            self.capabilities.clear_blocker("ACCOUNT_STATE_AUTHORITY")
        self._record_readiness_if_changed()

    def owned_token_shares(self, token_id: str) -> Decimal:
        """Shares of ``token_id`` this account provably owns through its own records.

        Execution sessions of this process (active, earlier attempts of the window,
        detached committed holds) and the settlement ledger's episodes (fills not sold;
        committed, resolving or settled holdings whose tokens may still be held). Both
        describe the same holdings, so the larger is used, never their sum.
        """
        session_ids = [self.lifecycle.session_id, *self.lifecycle.window_sessions,
                       *self.lifecycle.detached_sessions]
        from_sessions = Decimal(0)
        for session_id in dict.fromkeys(s for s in session_ids if s):
            state = self.coordinator.states.get(session_id)
            if state is None or state.identity is None or state.identity.token_id != token_id:
                continue
            from_sessions += state.confirmed_position_shares
        from_ledger = Decimal(0)
        hooks = getattr(self, "settlement_hooks", None)
        if hooks is not None:
            from_ledger = hooks.ledger.unsold_shares(token_id)
        return max(from_sessions, from_ledger)

    def _balance_explained_by_owned(self, blocker: str) -> bool:
        """A PRIOR_POSITION balance fully explained by owned positions is not unknown.

        Only the explained quantity is exempt: any excess (a balance above what this
        account's own records explain) keeps the blocker, i.e. UNKNOWN_INVENTORY.
        """
        if not blocker.startswith("PRIOR_POSITION:"):
            return False
        token_id, sep, qty_text = blocker[len("PRIOR_POSITION:"):].rpartition(":")
        if not sep or not token_id:
            return False
        try:
            balance = Decimal(qty_text)
        except Exception:  # noqa: BLE001
            return False
        owned = self.owned_token_shares(token_id)
        return owned > 0 and balance <= owned + self.config.lifecycle.dust_abs_shares

    async def _bind_pending_market(self) -> None:
        session = self.pending_market
        if session is None or session is self.active_market:
            return
        state = self.lifecycle.state
        if state is not None and not state.safe_to_open_entry:
            return
        if not self.can_promote():
            return
        if state is not None:
            self.lifecycle.release_terminal()
            self.selected_instrument = None
            self.selected_token_id = None
        previous_market_id = None
        if self.active_market is not None:
            previous_market = self.active_market.market
            previous_market_id = previous_market.condition_id or previous_market.market_id.value
            self._summarize_window(self.active_market.window_id, "promoted")
        self.lifecycle.new_window()
        self.protection_registry = ProtectionRegistry()
        self.pending_market = None
        self.capabilities.active_market_ready = False
        self.capabilities.account_reads_ready = False
        self.capabilities.user_stream_ready = False
        self.capabilities.prior_scope_clear = False
        self.capabilities.entry_allowance_ready = False
        self.capabilities.collateral_ready = False
        self.capabilities.order_metadata_ready = False
        await self.gateway.stop_user_stream()
        market = session.market
        market_id = market.condition_id or market.market_id.value
        token_ids = (market.yes.token_id.value, market.no.token_id.value)
        account_state = self.account_state_authority.current(market_id)
        self.active_market = session
        self.capabilities.active_market_ready = True
        self._apply_account_snapshot(account_state)
        self._refresh_order_metadata_capability()
        if not self.capabilities.order_metadata_ready:
            self._schedule_order_metadata_warm(session)
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
        blocking_foreign: list[ExecutionSessionState] = []
        for restored in foreign_restored:
            host_action = await self._inspect_retained_session(restored, bound_market_id=market_id)
            if host_action != "foreign_inventory_absent":
                blocking_foreign.append(restored)
        if blocking_foreign:
            self.prior_session_risk = True
            self.capabilities.set_blocker(
                "UNRESOLVED_PRIOR_MARKET_SESSION",
                ",".join(str(restored.session_id) for restored in blocking_foreign),
            )
        else:
            self.capabilities.clear_blocker("UNRESOLVED_PRIOR_MARKET_SESSION")
        for restored in matching_restored:
            await self._inspect_retained_session(restored, bound_market_id=market_id)
        if len(matching_restored) > 1:
            self.prior_session_risk = True
            self.capabilities.set_blocker("MULTIPLE_RESTORED_SESSIONS")
        else:
            self.capabilities.clear_blocker("MULTIPLE_RESTORED_SESSIONS")
        if matching_restored:
            self.prior_session_risk = True
        restored = matching_restored[0] if len(matching_restored) == 1 else None
        self.stream_session_id = (
            str(restored.session_id)
            if restored is not None
            else f"{self.config.run_name}-{uuid4()}"
        )
        self.lifecycle.stream_alias = self.stream_session_id
        self._window_started_at[session.window_id] = self._corrected_now().isoformat()
        self._seed_driver_from_ledger(session.window_id)
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
                self.capabilities.set_blocker(
                    "STARTUP_ACTION_REQUIRED",
                    "owned exposure remains after startup assessment",
                )
                self.stop_requested = True
            elif reconciled.has_unresolved_mutation_attempt:
                await self.lifecycle.mark_manual("ambiguous_prior_submission")
                self.capabilities.set_blocker("AMBIGUOUS_PRIOR_SUBMISSION")
                self.stop_requested = True
            else:
                self.stop_requested = bool(reconciled is not None and reconciled.terminal)

    async def _cancel_for_hold(self) -> Any:
        """Cancel owned resting orders and return the reconciled state (no sells)."""
        if self.lifecycle.session_id is None:
            # The committed hold was already detached (continuous run): no session and no
            # resting order remain; the settlement ledger owns the position.
            return None
        await self.lifecycle._cancel_open_orders(reason="hold_to_resolution")
        return await self.lifecycle.reconcile()

    def _strategy_fee_curve(self) -> Any:
        runtime = self._market_runtime_ref
        if runtime is None or self.active_market is None:
            return None
        driver = runtime._strategy_by_window.get(self.active_market.window_id) or runtime.driver
        return getattr(driver, "fee_curve", None)

    async def _settlement_before_entry(
        self, intent: Any, token_id: str, instrument: Any, *, account_state: Any = None
    ) -> tuple[str, ...] | None:
        """Settlement gate. Returns the blocking reasons (recorded), or None when reserved."""
        assert self.settlement_hooks is not None and self.active_market is not None
        episode_seq, attempt_seq = self._intent_episode(intent)
        collateral = None if account_state is None else getattr(
            account_state, "collateral_balance", None
        )
        session = self.active_market
        market = session.market
        binding = getattr(session, "binding", None)
        outcome_index = None
        for leg in () if binding is None else binding.outcomes.legs:
            if leg.token_id.value == token_id:
                outcome_index = leg.outcome_index
        if outcome_index is None:
            check_reasons: tuple[str, ...] = ("OUTCOME_INDEX_UNKNOWN",)
            evidence: dict[str, Any] = {}
        else:
            check = await self.settlement_hooks.before_entry(
                window_id=session.window_id,
                window_slug=session.window_id,
                market_id=market.market_id.value,
                condition_id=market.condition_id,
                token_id=token_id,
                outcome_index=outcome_index,
                target_notional=intent.target_notional,
                tick_size=market.tick_size,
                min_order_size=market.min_order_size,
                strategy_fee_curve=self._strategy_fee_curve(),
                episode_seq=episode_seq,
                attempt_seq=attempt_seq,
                available_collateral=collateral,
                collateral_read_started_at=None
                if account_state is None
                else getattr(account_state, "collateral_read_started_at", None),
            )
            if check.allowed:
                self.run_recorder.record("SETTLEMENT_EPISODE_RESERVED", check.evidence)
                return None
            check_reasons, evidence = check.reasons, check.evidence
        self.run_recorder.record(
            "INTENT_BLOCKED",
            {
                "intent_id": intent.intent_id.value,
                "intent_type": type(intent).__name__,
                "reason": "SETTLEMENT_ENTRY_BLOCKED",
                "settlement_reasons": list(check_reasons),
                **evidence,
            },
        )
        return tuple(check_reasons)

    def _position_cost(self, state: ExecutionSessionState | None) -> Decimal:
        if state is None:
            return Decimal("0")
        return sum(
            (trade.shares * trade.price for trade in state.trades.values() if trade.side == "BUY"),
            Decimal("0"),
        )

    def note_assessment_validity(self) -> None:
        """Local validity only. Strategy evaluation does not call the account."""
        deadline = self._assessment_valid_until
        if deadline is None or self.time_authority is None:
            return
        now = self.time_authority.now_corrected_utc()
        if now >= deadline:
            self.capabilities.set_blocker(
                "STARTUP_ASSESSMENT_EXPIRED",
                "startup evidence is older than its validity window",
            )

    def apply_startup_recheck(self, ready: bool) -> None:
        if ready:
            self.capabilities.clear_blocker("STARTUP_ASSESSMENT_EXPIRED")
            self.capabilities.clear_blocker("STARTUP_CHECK_PENDING")
            if self.time_authority is not None:
                from datetime import timedelta

                self._assessment_valid_until = (
                    self.time_authority.now_corrected_utc()
                    + timedelta(seconds=self._startup_validity_window_s())
                )
        else:
            self.capabilities.set_blocker(
                "STARTUP_CHECK_PENDING",
                "startup recheck is not ready",
            )

    def on_evaluation(self, market_runtime: ZGapMarketRuntime) -> list[dict[str, Any]]:
        self._market_runtime_ref = market_runtime
        ready = market_runtime.prepare_aligned_eval()
        if not ready.ok or ready.snapshot is None or ready.binding is None:
            self.capabilities.model_ready = False
            self.run_recorder.record(
                "EVALUATION_SKIPPED",
                {"reasons": list(ready.skip_reasons)},
            )
            if getattr(self, "decision_recording", None) is not None:
                self._record_skipped_decision(ready, market_runtime)
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
            references=ReferenceFacts(
                chainlink_raw=None if ready.dyn is None else ready.dyn.chainlink_raw,
                chainlink_fresh=ready.dyn is not None and ready.dyn.chainlink_raw is not None,
                sealed_k=None if ready.sealed is None else ready.sealed.ptb_k,
                trading_reference_raw=ready.binance_raw,
                chainlink_source_ts=None if ready.dyn is None else ready.dyn.chainlink_source_ts,
            ),
            last_exit_reason=(
                None
                if ready.session is None
                else self._last_exit_reason.get(ready.session.window_id)
            ),
        )
        graph = getattr(market_runtime, "indicator_graph", None)
        features = graph.snapshot() if graph is not None else FeatureBundle()
        result = ready.binding.evaluate(
            market_snapshot=ready.snapshot,
            causation_id=ready.snapshot.causation_id,
            correlation_id=ready.snapshot.correlation_id,
            trigger="feed",
            context=context,
            features=features,
        )
        self.capabilities.model_ready = result.eligibility.model_ready
        decision_evidence = dict(result.decision.evidence)
        if getattr(self, "decision_recording", None) is not None:
            self._record_evaluated_decision(ready, result, market_runtime)
            # The full row lives in the decision recording; keep the journal compact.
            decision_evidence = _compact_decision_evidence(decision_evidence)
        strategy_blockers = result.eligibility.blockers
        strategy_inputs_eligible = result.eligibility.strategy_inputs_eligible
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

    # -- decision recording (recording.host) ---------------------------------------
    def _open_recording(self) -> None:
        settings = getattr(self.config, "recording", None)
        if settings is None:
            return
        from tyrex_pm.recording.host import open_decision_recording
        from tyrex_pm.strategies.registry import get_strategy_plugin

        try:
            plugin = get_strategy_plugin(self.config.strategy_kind)
            schema_hook = getattr(plugin, "decision_record_schema", None)
            schema = None if schema_hook is None else schema_hook(self.config.strategy)
            try:
                band = tuple(float(v) for v in plugin.entry_tau_bounds(self.config.strategy))
            except Exception:  # noqa: BLE001 - the explain band is optional
                band = None
            repro = self.reproducibility or {}
            self.decision_recording = open_decision_recording(
                settings,
                output_directory=self.output_directory,
                mode="paper" if getattr(self.config, "venue", "live") == "paper" else "live",
                run_id=self.run_instance_id,
                schema=schema,
                entry_band=band,
                manifest={
                    "configured_run_name": self.config.run_name,
                    "strategy_kind": self.config.strategy_kind,
                    "reproducibility": repro,
                    "clock_policy": {
                        "max_uncertainty_ms": max_clock_uncertainty_ms(self.config),
                    },
                    "feature_policies": [
                        getattr(spec, "instance_id", str(spec))
                        for spec in self._required_feature_specs
                    ],
                    "evaluation_interval_s": self.config.market.evaluation_interval_s,
                },
            )
        except Exception as exc:  # noqa: BLE001 - recording never blocks trading
            self.decision_recording = None
            self.recording_status = {"failed": True,
                                     "error": f"{type(exc).__name__}: {exc}"}
            self.run_recorder.record("RECORDING_FAILED", {"stage": "open",
                                                          **self.recording_status})
            return
        if self.decision_recording is not None:
            self.run_recorder.record("RECORDING_STARTED", {
                "directory": str(self.decision_recording.recorder.directory),
                "level": settings.level.value,
                "explain_trigger": settings.explain_trigger.value,
            })

    def _recording_time_view(self) -> Any:
        authority = self.time_authority
        try:
            return None if authority is None else authority.view()
        except Exception:  # noqa: BLE001
            return None

    def _record_skipped_decision(self, ready: Any, market_runtime: Any) -> None:
        recording = self.decision_recording
        if recording is None:
            return
        from tyrex_pm.runtime.decision_records import skipped_record

        session = ready.session or getattr(market_runtime, "active", None)
        at_ns, end_ns = self._recording_times(ready, session)
        recording.on_skipped(
            ready, skipped_record(ready, at_ns=at_ns, window_end_ns=end_ns),
            window_id=None if session is None else session.window_id,
            time_view=self._recording_time_view())

    def _record_evaluated_decision(self, ready: Any, result: Any, market_runtime: Any) -> None:
        from tyrex_pm.runtime.decision_records import evaluated_record

        session = ready.session
        at_ns, end_ns = self._recording_times(ready, session)
        self.decision_recording.on_evaluated(
            ready, evaluated_record(ready, result, at_ns=at_ns, window_end_ns=end_ns),
            window_id=None if session is None else session.window_id,
            time_view=self._recording_time_view(),
            graph=getattr(market_runtime, "indicator_graph", None))

    def _recording_times(self, ready: Any, session: Any) -> tuple[int, int | None]:
        snapshot = getattr(ready, "snapshot", None)
        if snapshot is not None:
            at = snapshot.observed_at
        elif self.time_authority is not None:
            at = self.time_authority.now_corrected_utc()
        else:
            at = datetime.now(timezone.utc)
        end = None if session is None else getattr(session.market, "event_end", None)
        return (int(at.timestamp() * 1e9),
                None if end is None else int(end.timestamp() * 1e9))

    def _recording_heartbeat(self, *, force: bool = False) -> None:
        """RECORDING_STATUS every 60 s; RECORDING_FAILED once when a stream fails."""
        recording = self.decision_recording
        if recording is None:
            return
        now = time.monotonic()
        if not force and now - self._recording_status_mono < RECORDING_STATUS_INTERVAL_S:
            return
        self._recording_status_mono = now
        status = recording.status()
        status["run_evidence"] = self.run_recorder.status()
        self.recording_status = status
        self.run_recorder.record("RECORDING_STATUS", _recording_status_event(status))
        if status.get("failed") and not self._recording_failed_reported:
            self._recording_failed_reported = True
            self.run_recorder.record("RECORDING_FAILED", {
                "stage": "write",
                "writer_error": status.get("writer_error"),
                "streams": {name: snap.get("last_error")
                            for name, snap in status.get("streams", {}).items()
                            if snap.get("state") == "failed"},
                "effect": "decision recording stopped; trading continues",
            })

    def _check_raw_capture(self) -> None:
        """Apply ``recording.raw_on_fault`` once when raw capture becomes incomplete.

        ``stop_capture`` (default): capture stops visibly, trading continues.
        ``stop_entries`` (strict qualification runs): also block new entries; exits,
        holds and position management are unaffected (entry-only blocker).
        """
        recording = self.decision_recording
        if self._raw_fault_handled or recording is None or recording.raw is None:
            return
        reason = recording.raw_fault()
        if reason is None:
            return
        self._raw_fault_handled = True
        recording.stop_raw(reason)
        policy = self.config.recording.raw_on_fault
        if policy == "stop_entries":
            self.capabilities.set_blocker("RAW_CAPTURE_FAILED", reason)
        self.run_recorder.record("RAW_CAPTURE_FAILED", {
            "reason": reason, "policy": policy,
            "effect": "raw capture stopped; new entries blocked, positions managed"
            if policy == "stop_entries"
            else "raw capture stopped; trading continues",
        })
        self._record_readiness_if_changed()

    def _close_recording(self) -> None:
        recording = self.decision_recording
        if recording is None:
            return
        try:
            status = recording.close()
        except Exception as exc:  # noqa: BLE001
            status = {"failed": True, "error": f"{type(exc).__name__}: {exc}"}
        status["run_evidence"] = self.run_recorder.status()
        self.recording_status = status
        self.run_recorder.record("RECORDING_CLOSED", _recording_status_event(status))

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

    # -- episode / attempt plumbing ----------------------------------------------------

    @property
    def continuous(self) -> bool:
        market = getattr(getattr(self, "config", None), "market", None)
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

    def _driver_for(self, window_id: str | None) -> Any:
        runtime = getattr(self, "_market_runtime_ref", None)
        if runtime is None or not hasattr(runtime, "window_driver"):
            return None
        return runtime.window_driver(window_id)

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
        submit_ns = getattr(self, "_entry_submit_started_ns", {}).get(intent.intent_id.value)

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
            stages = getattr(getattr(self, "coordinator", None), "submit_timings", {}).get(
                getattr(order.spec, "order_id", None), {}
            )
            for key in ("prepare_ms", "final_gate_ms", "authorize_ms", "post_ms"):
                latency[key] = stages.get(key)
        monitor = getattr(self, "loop_lag_monitor", None)
        latency["event_loop_lag_recent_max_ms"] = (
            None if monitor is None else monitor.recent_max_ms()
        )
        gateway = getattr(self, "gateway", None)
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
        getattr(self, "_entry_submit_started_ns", {}).pop(intent.intent_id.value, None)
        self.run_recorder.record("ENTRY_OUTCOME", payload)
        if outcome.retry_class is RetryClass.PAUSE_ENTRIES:
            self.run_recorder.record(
                "ENTRIES_PAUSED_FOR_WINDOW",
                {"window_id": window_id, "reason": outcome.reason},
            )
        driver = self._driver_for(window_id)
        handler = getattr(driver, "on_entry_outcome", None)
        if handler is not None:
            handler(outcome)

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
        released_at = getattr(self, "_last_release_at", None)
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
        if getattr(self, "settlement_hooks", None) is not None:
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
        if getattr(self, "settlement_hooks", None) is not None and self.active_market is not None:
            episode_seq = self._intent_episode(intent)[0]
            settlement_outcome = self.settlement_hooks.after_entry(
                window_id=self.active_market.window_id,
                state=state,
                cost_basis=self._position_cost(state),
                episode_seq=episode_seq,
                posted=outcome.posted if self._multi_attempt() else None,
                prepare_failed=outcome.prepare_failed,
            )
            self.run_recorder.record("SETTLEMENT_ENTRY_OUTCOME", {"outcome": settlement_outcome})
        self._deliver_entry_outcome(intent, outcome, state)
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
        handler = getattr(driver, "on_exit_outcome", None)
        if handler is not None:
            handler(
                ExitOutcome(
                    kind,
                    intent.intent_id.value,
                    self._corrected_now(),
                    reason=reason,
                    remaining_shares=Decimal("0")
                    if state is None
                    else state.confirmed_position_shares,
                    sold_shares=sold or Decimal("0"),
                )
            )

    async def _consume_hold(self, intent: HoldToResolutionIntent) -> None:
        await self._commit_hold(
            intent.window_id,
            source="strategy_intent",
            intent_id=intent.intent_id.value,
            extra={
                "reason_code": intent.reason_code,
                "market_id": intent.market_id.value,
                "instrument_id": intent.instrument_id.value,
            },
        )

    def _host_commits_hold(self) -> bool:
        """Hold profile: the host commits a filled position itself (no strategy turn)."""
        if self.settlement_hooks is None or getattr(self.config, "lifecycle_mode", None) != (
            "hold_to_resolution"
        ):
            return False
        strategy_exit = getattr(getattr(self.config, "strategy", None), "exit", None)
        return getattr(strategy_exit, "policy", None) == "hold_to_resolution"

    async def _commit_hold(
        self,
        window_id: str,
        *,
        source: str,
        intent_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Commit the window's filled episode to resolution (idempotent).

        Keeps every precondition of ``SettlementHooks.commit_hold`` (confirmed inventory
        matching the ledger, conflicting orders cancelled, no unresolved submission).
        A transient refusal is retried on the next tick; a committed hold is never sold
        and survives rollover and shutdown (detached to the settlement ledger).
        """
        committed, detail = (False, "LIFECYCLE_NOT_SETTLEMENT_PROFILE")
        if self.settlement_hooks is not None:
            committed, detail = await self.settlement_hooks.commit_hold(
                window_id=window_id,
                state=self.lifecycle.state,
                cancel_conflicting=self._cancel_for_hold,
            )
        if committed or getattr(self, "_hold_commit_noted", None) != (window_id, detail):
            self._hold_commit_noted = (window_id, detail)
            self.run_recorder.record(
                "HOLD_TO_RESOLUTION",
                {"intent_id": intent_id, "window_id": window_id, "source": source,
                 "committed": committed, "detail": detail, **(extra or {})},
            )
        if not committed:
            if source == "host_after_fill" and detail in _TRANSIENT_HOLD_REFUSALS:
                self._pending_hold_commit = window_id
            return False
        self._pending_hold_commit = None
        driver = self._driver_for(window_id)
        note = getattr(driver, "on_hold_committed", None)
        if callable(note):
            note()
        state = self.lifecycle.state
        if state is not None:
            self.protection_registry.disarm(state.session_id)
        if self.continuous:
            # The holding now belongs to the settlement ledger (resolution worker); the
            # lifecycle is freed for the next window once its trades are confirmed.
            self._pending_detach = window_id
            await self._try_detach()
            return True
        # The single episode is complete for this process; the holding now
        # waits for resolution under `tyrex-pm settlement`.
        self.stop_requested = True
        return True

    async def _try_detach(self) -> bool:
        """Detach a committed hold once nothing about it is pending (e.g. a matched trade
        still awaiting venue confirmation). Retried every tick; until then the window
        cannot be promoted, and the hold stays protected from any sale."""
        window = getattr(self, "_pending_detach", None)
        if window is None or self.lifecycle.state is None:
            self._pending_detach = None
            return True
        state = await self.lifecycle.reconcile()
        # The tick and the strategy path both retry the detach; the other one may have
        # finished it while this one awaited the reconciliation.
        if getattr(self, "_pending_detach", None) != window or self.lifecycle.state is None:
            return True
        try:
            detached = self.lifecycle.detach_committed(reason="hold_committed")
        except RuntimeError as exc:
            if getattr(self, "_detach_noted", None) != (window, str(exc)):
                self._detach_noted = (window, str(exc))
                self.run_recorder.record(
                    "COMMITTED_HOLD_DETACH_PENDING",
                    {"window_id": window, "reason": str(exc),
                     "phase": None if state is None else state.phase.value},
                )
            return False
        self.run_recorder.record(
            "COMMITTED_HOLD_DETACHED", {"session_id": detached, "window_id": window}
        )
        self._pending_detach = None
        self.selected_instrument = None
        self.selected_token_id = None
        return True

    async def on_async_tick(self, market_runtime: ZGapMarketRuntime) -> None:
        pending_hold = getattr(self, "_pending_hold_commit", None)
        if pending_hold is not None:
            await self._commit_hold(pending_hold, source="host_after_fill")
        if getattr(self, "_pending_detach", None) is not None:
            await self._try_detach()
        await self._bind_pending_market()
        if getattr(self, "decision_recording", None) is not None:
            self._check_raw_capture()
            self._recording_heartbeat()
        worker = getattr(self, "settlement_worker", None)
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
        check_earlier = getattr(self, "_check_earlier_attempts", None)
        if check_earlier is not None:
            await check_earlier()

    def _seed_driver_from_ledger(self, window_id: str) -> None:
        """Restart in the middle of a window: restore its durable attempt counters."""
        hooks = self.settlement_hooks
        driver = self._driver_for(window_id)
        restore = getattr(driver, "restore_window_usage", None)
        if hooks is None or restore is None:
            return
        usage = hooks.ledger.window_usage(window_id)
        if usage.episodes == 0:
            return
        latest = hooks.ledger.by_window(window_id)
        restore(
            episodes=usage.episodes,
            attempts=usage.attempts,
            posts=usage.posts,
            filled_episodes=usage.filled_episodes,
            latest_attempts=0 if latest is None else latest.attempts,
            latest_state=None if latest is None or latest.state is None else latest.state.value,
        )
        self.run_recorder.record(
            "WINDOW_BUDGET_RESTORED",
            {"window_id": window_id, "episodes": usage.episodes, "attempts": usage.attempts,
             "posts": usage.posts},
        )

    async def _check_earlier_attempts(self) -> None:
        """Late evidence for an earlier attempt of this window is a real exposure.

        It is attributed to its original session (stream routing), reconciled, and new
        entries stay blocked (``PRIOR_ATTEMPT_UNRESOLVED``) until it is resolved; a late
        fill on an attempt the ledger closed as no-fill needs an operator decision.
        """
        # Released attempts are polled (throttled) for a while: a fill reported only by
        # REST must not go unnoticed just because the stream missed it.
        for session_id in self.lifecycle.sessions_to_watch(
            watch_s=LATE_EVIDENCE_WATCH_S, interval_s=LATE_EVIDENCE_POLL_S
        ):
            try:
                await self.lifecycle.reconciler.reconcile(self.coordinator, session_id)
            except Exception as exc:  # noqa: BLE001
                self._record_runtime_error("earlier_attempt_watch", exc)
        unresolved = self.lifecycle.unresolved_window_sessions()
        if not unresolved:
            self.capabilities.clear_blocker("PRIOR_ATTEMPT_UNRESOLVED")
            return
        self.capabilities.set_blocker(
            "PRIOR_ATTEMPT_UNRESOLVED", ",".join(str(s.session_id) for s in unresolved)
        )
        for state in unresolved:
            if state.session_id is None:
                continue
            try:
                await self.lifecycle.reconciler.reconcile(self.coordinator, state.session_id)
            except Exception as exc:  # noqa: BLE001
                self._record_runtime_error("earlier_attempt_reconcile", exc)
            current = self.coordinator.states.get(state.session_id)
            if current is not None and current.has_exposure:
                self.run_recorder.record(
                    "LATE_FILL_DETECTED",
                    {"session_id": state.session_id,
                     "shares": str(current.confirmed_position_shares)},
                )
                hooks = self.settlement_hooks
                if hooks is not None and self.active_market is not None:
                    episode = hooks.current_episode(self.active_market.window_id)
                    if episode is not None and episode.state is not None and (
                        episode.state.value in ("NO_FILL", "NO_DISPATCH")
                    ):
                        hooks.ledger.record(
                            "MANUAL_INTERVENTION",
                            episode.episode_id,
                            key=f"MANUAL:late_fill:{state.session_id}",
                            reason="late_fill_after_no_fill",
                        )

    async def _shutdown_residual_hold(self, reason: str) -> bool:
        """managed_exit: at stop, remaining inventory follows the declared residual policy
        (hold to resolution) instead of an unannounced sale. Falls back to the drain."""
        if self.config.lifecycle_mode != "managed_exit" or self.settlement_hooks is None:
            return False
        state = self.lifecycle.state
        if state is None or not state.has_exposure or self.active_market is None:
            return False
        committed, detail = await self.settlement_hooks.commit_hold(
            window_id=self.active_market.window_id,
            state=state,
            cancel_conflicting=self._cancel_for_hold,
        )
        self.run_recorder.record(
            "SHUTDOWN_RESIDUAL_HOLD",
            {"reason": reason, "committed": committed, "detail": detail},
        )
        return committed

    def _summarize_window(self, window_id: str, reason: str) -> None:
        if any(row["window_id"] == window_id for row in self.window_summaries):
            return
        from tyrex_pm.reporting.timezones import paris_iso

        now = self._corrected_now()
        summary: dict[str, Any] = {
            "window_id": window_id,
            "closed_by": reason,
            "bound_at": self._window_started_at.get(window_id),
            "summarized_at": now.isoformat(),
            "summarized_at_paris": paris_iso(now),
            "accounting_day_utc": now.date().isoformat(),
        }
        hooks = self.settlement_hooks
        if hooks is not None:
            usage = hooks.ledger.window_usage(window_id)
            # Ledger counts: an entry attempt is one authorized intent; it either reached
            # the venue (a POST) or was refused before sending (preparation failure or
            # final-gate refusal).
            summary.update(
                episodes=usage.episodes,
                filled_episodes=usage.filled_episodes,
                entry_attempts=usage.attempts,
                venue_posts=usage.posts,
                never_sent_attempts=max(0, usage.attempts - usage.posts),
                prepare_failures=usage.prepare_failures,
                states=[
                    e.state.value if e.state else None
                    for e in hooks.ledger.window_episodes(window_id)
                ],
            )
        driver = self._driver_for(window_id)
        episode_state = getattr(driver, "state", None)
        # Only report strategy state that belongs to this window (the driver may already
        # have been reset for the next one).
        if (
            episode_state is not None
            and hasattr(episode_state, "phase")
            and getattr(episode_state, "window_id", window_id) == window_id
        ):
            summary.update(
                strategy_phase=episode_state.phase.value,
                strategy_closed_reason=getattr(episode_state, "closed_reason", None),
                strategy_window_attempts=getattr(episode_state, "window_attempts", None),
            )
        self.window_summaries.append(summary)
        self.run_recorder.record("WINDOW_SUMMARY", summary)
        try:
            import json

            path = self.output_directory / "windows.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(summary, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - reporting must not stop trading
            self._record_runtime_error("window_summary", exc)

    def readiness_dict(self) -> dict[str, Any]:
        state = self.lifecycle.state
        snap = self.capabilities.snapshot(exposed=bool(state and state.has_exposure))
        return {
            "blockers": [{"code": value} for value in snap.blockers],
            **snap.__dict__,
        }

    def _release_state_lock(self) -> None:
        lock = self._state_lock
        self._state_lock = None
        if lock is not None:
            lock.release()

    def startup_buy_block(self) -> str | None:
        """Local freshness check used only at final BUY authorization."""
        if self._startup_invalidated_reason:
            return self._startup_invalidated_reason
        if self._assessment_generation not in {None, self.run_instance_id}:
            return "STARTUP_ASSESSMENT_EXPIRED"
        until = self._assessment_valid_until
        if until is None:
            return "STARTUP_ASSESSMENT_EXPIRED"
        now = (
            datetime.now(timezone.utc)
            if self.time_authority is None
            else self.time_authority.now_corrected_utc()
        )
        if now >= until:
            return "STARTUP_ASSESSMENT_EXPIRED"
        return None

    def note_startup_stream_gap(self) -> None:
        self._startup_invalidated_reason = "STARTUP_STREAM_GAP"
        self._assessment_valid_until = None
        self.capabilities.set_blocker(
            "STARTUP_ASSESSMENT_EXPIRED",
            "the book stream gap invalidated startup readiness",
        )

    def _mark_startup_fresh(self) -> None:
        from datetime import timedelta

        self._startup_invalidated_reason = None
        self._assessment_generation = self.run_instance_id
        self.capabilities.clear_blocker("STARTUP_ASSESSMENT_EXPIRED")
        self.capabilities.clear_blocker("STARTUP_CHECK_PENDING")
        now = (
            datetime.now(timezone.utc)
            if self.time_authority is None
            else self.time_authority.now_corrected_utc()
        )
        self._assessment_valid_until = now + timedelta(seconds=self._startup_validity_window_s())

    def _startup_validity_window_s(self) -> float:
        """Keep the previous assessment alive through one inspection budget."""
        return (
            self.config.startup.monitor_interval_s + self.config.startup.inspection_budget_s
        )

    def _corrected_now(self) -> datetime:
        authority = getattr(self, "time_authority", None)
        if authority is None:
            return datetime.now(timezone.utc)
        return authority.now_corrected_utc()

    def _seconds_until_next_inspection(self) -> float:
        """Start the next read one budget before the current assessment expires."""
        until = self._assessment_valid_until
        if until is None:
            return self.config.startup.monitor_interval_s
        remaining = (until - self._corrected_now()).total_seconds()
        if remaining <= 0:
            return 0.0
        lead = remaining - self.config.startup.inspection_budget_s
        if lead > 0:
            return lead
        return min(self.config.startup.monitor_interval_s, remaining)

    async def _startup_monitor_loop(self) -> None:
        while not self.stop_requested:
            delay = self._seconds_until_next_inspection()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.stop_requested:
                return
            expired = False
            until = self._assessment_valid_until
            if until is not None and self._corrected_now() >= until:
                expired = True
            try:
                assessment = await self._assess_startup(write_report=False)
            except Exception as exc:  # noqa: BLE001 - recorded, readiness stays blocked
                reason = self._record_recheck_failure([], error=f"{type(exc).__name__}: {exc}")
                self._invalidate_startup_recheck(
                    "STARTUP_ASSESSMENT_EXPIRED",
                    f"startup recheck failed: {reason}",
                )
                continue
            publication = self._last_inspection_publication
            if publication in {"discarded", "withheld"}:
                if expired:
                    self._invalidate_startup_recheck(
                        "STARTUP_ASSESSMENT_EXPIRED",
                        "startup recheck could not be published before it expired",
                    )
                continue
            blocking = self._recheck_blocking_issues(assessment)
            if publication == "accepted" and not blocking:
                self._last_recheck_failure_key = None
                self._mark_startup_fresh()
            else:
                reason = self._record_recheck_failure(
                    blocking,
                    error=None if publication == "accepted" else f"publication:{publication}",
                )
                self._invalidate_startup_recheck(
                    "STARTUP_CHECK_PENDING",
                    f"startup recheck is not ready: {reason}",
                )

    def _invalidate_startup_recheck(self, blocker: str, message: str) -> None:
        self._startup_invalidated_reason = "STARTUP_RECHECK_FAILED"
        self._assessment_valid_until = None
        self.capabilities.set_blocker(blocker, message)

    def _recheck_blocking_issues(self, assessment: Any) -> list[Any]:
        """Issues that fail a startup recheck (not AUTO, not the active session's own exposure)."""
        if assessment.ready:
            return []
        active = self.lifecycle.session_id
        blocking = []
        for issue in assessment.issues:
            if issue.severity.value == "AUTO":
                continue
            if (
                issue.code in {"OWNED_POSITION", "OWNED_LIVE_ORDER"}
                and issue.session_id == active
            ):
                continue
            blocking.append(issue)
        return blocking

    def _recheck_keeps_readiness(self, assessment: Any) -> bool:
        return not self._recheck_blocking_issues(assessment)

    def _record_recheck_failure(self, issues: list[Any], *, error: str | None = None) -> str:
        """Record why a startup recheck failed (once per distinct cause); return a summary."""
        details = [
            {
                "code": issue.code,
                "severity": issue.severity.value,
                "session_id": issue.session_id,
                "summary": issue.summary,
            }
            for issue in issues
        ]
        key = (error, tuple(sorted((d["code"], d["session_id"] or "") for d in details)))
        if key != self._last_recheck_failure_key:
            self._last_recheck_failure_key = key
            self.run_recorder.record(
                "STARTUP_RECHECK_FAILED",
                {"issues": details, "error": error},
            )
        codes = ", ".join(
            f"{d['code']}({(d['session_id'] or '-')[-8:]})" for d in details
        )
        return codes or (error or "unknown")

    def _sessions_for_inspection(self):
        """Restored sessions plus anything the running coordinator already owns."""
        by_id = {}
        for session in self.restored_sessions:
            if session.session_id:
                by_id[session.session_id] = session
        for session_id, session in self.coordinator.states.items():
            by_id[session_id] = session
        return tuple(by_id.values())

    def _merge_inspection_sessions(self, reported):
        merged = []
        seen: set[str] = set()
        for session in reported:
            current = self.coordinator.states.get(session.session_id or "")
            chosen = session if current is None else current
            session_id = chosen.session_id
            if session_id and session_id in seen:
                continue
            if session_id:
                seen.add(session_id)
            merged.append(chosen)
        for session in self._sessions_for_inspection():
            session_id = session.session_id
            if session_id and session_id not in seen:
                merged.append(session)
                seen.add(session_id)
        return tuple(merged)

    async def _assess_startup(self, *, write_report: bool = True):
        from dataclasses import replace
        from uuid import uuid4

        from tyrex_pm.execution.activity_checkpoint import save_activity_ledger
        from tyrex_pm.execution.inspection_sync import (
            authored_publication_holds,
            publication_decision,
        )
        from tyrex_pm.execution.startup_reconciliation import (
            AccountIdentity,
            InspectionFacts,
            ReadCoverage,
            assess,
            evidence_disposition,
        )
        from tyrex_pm.reporting.startup_report import startup_report_payload, write_startup_report
        from tyrex_pm.runtime.startup_controller import StartupController

        started_epoch = _try_execution_epoch(self.coordinator)
        facts = await self._read_startup_facts()
        owned_sessions = self._sessions_for_inspection()
        if facts is None:
            facts = InspectionFacts(
                identity=None,
                coverage=ReadCoverage(
                    complete=False,
                    positions_complete=False,
                    orders_complete=False,
                    activity_complete=False,
                    reason="startup inspection exceeded its budget",
                ),
                sessions=owned_sessions,
            )
        else:
            facts = replace(facts, sessions=self._merge_inspection_sessions(facts.sessions))
        if facts.identity is None and facts.coverage.complete:
            facts = replace(
                facts,
                identity=AccountIdentity(
                    chain_id=137, environment="production", wallet="unspecified"
                ),
            )
        if self.settlement_hooks is not None:
            facts = replace(
                facts,
                committed_holds=self.settlement_hooks.committed_holds(
                    include_settled=self.continuous
                ),
                committed_holds_block_entries=not self.continuous,
            )
        self._startup_wallet = None if facts.identity is None else facts.identity.wallet
        controller = StartupController(
            coordinator=self.coordinator,
            generation=self.run_instance_id,
        )
        await self.coordinator.start()
        async with self.coordinator.evidence_gate:
            ended_epoch = _try_execution_epoch(self.coordinator)
            if started_epoch is None or ended_epoch is None:
                execution = "discarded"
            else:
                execution = publication_decision(started_epoch, ended_epoch)
            evidence = evidence_disposition(facts)
            publication = execution if execution != "accepted" else evidence
            checkpoint = "unchanged"
            if publication != "accepted":
                assessment = assess(facts)
            else:
                before_unresolved = ended_epoch.unresolved_attempt_ids
                before_sequence = ended_epoch.journal_sequence
                assessment = await controller.inspect(facts)
                after_epoch = _try_execution_epoch(self.coordinator)
                observed = frozenset(
                    self.coordinator.journal.event_ids_after(before_sequence)
                )
                consistent = after_epoch is not None and authored_publication_holds(
                    before_unresolved=before_unresolved,
                    after_unresolved=after_epoch.unresolved_attempt_ids,
                    authored_event_ids=frozenset(controller.published_event_ids),
                    observed_event_ids=observed,
                )
                ledger = facts.activity_ledger
                if consistent and ledger is not None:
                    save_activity_ledger(self.coordinator.journal, ledger)
                    checkpoint = "advanced"
                elif not consistent:
                    publication = "discarded"
        self._last_inspection_publication = publication
        self._record_inspection_timing(
            facts,
            publication,
            operation_id=str(uuid4()),
            execution=execution,
            evidence=evidence,
            checkpoint=checkpoint,
            ready=bool(assessment.ready and publication == "accepted"),
        )
        if write_report:
            payload = startup_report_payload(
                assessment,
                run_name=self.config.run_name,
                live_requested=self.capabilities.live_requested,
            )
            self.startup_report = payload
            write_startup_report(self.output_directory / "startup_report.json", payload)
        return assessment

    def _record_inspection_timing(
        self,
        facts: Any,
        publication: str,
        *,
        operation_id: str,
        execution: str,
        evidence: str,
        checkpoint: str,
        ready: bool,
    ) -> None:
        coverage = None if facts is None else getattr(facts, "coverage", None)
        duration_ms = None if facts is None else getattr(facts, "inspection_duration_ms", None)
        pages = 0 if facts is None else int(getattr(facts, "activity_pages", 0) or 0)
        mode = "" if facts is None else str(getattr(facts, "activity_mode", "") or "")
        payload = {
            "operation_id": operation_id,
            "duration_ms": duration_ms,
            "activity_pages": pages,
            "activity_mode": mode,
            "execution": execution,
            "evidence": evidence,
            "publication": publication,
            "checkpoint": checkpoint,
            "ready": ready,
            "activity_complete": bool(coverage is not None and coverage.activity_complete),
            "coverage_complete": bool(coverage is not None and coverage.complete),
        }
        try:
            self.run_recorder.record("INSPECTION_TIMING", payload)
        except Exception:
            return

    async def _read_startup_facts(self):
        import inspect

        reader = getattr(self.gateway, "startup_inspection", None)
        if reader is None:
            from tyrex_pm.execution.startup_reconciliation import InspectionFacts, ReadCoverage

            return InspectionFacts(
                identity=None,
                coverage=ReadCoverage(
                    complete=False,
                    positions_complete=False,
                    orders_complete=False,
                    activity_complete=False,
                    reason="startup reader is not available",
                ),
                sessions=self._sessions_for_inspection(),
            )
        kwargs: dict[str, Any] = {}
        parameters = inspect.signature(reader).parameters
        if "journal" in parameters:
            kwargs["journal"] = self.coordinator.journal
        if "sessions" in parameters:
            kwargs["sessions"] = self._sessions_for_inspection()

        async def _call():
            result = reader(**kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

        try:
            return await asyncio.wait_for(_call(), timeout=self.config.startup.inspection_budget_s)
        except TimeoutError:
            return None

    async def _resolve_startup_action(self, assessment: Any) -> Any:
        from tyrex_pm.application.startup_interaction import proposal_for, request_decision
        from tyrex_pm.core.ids import CorrelationId, MarketId, StrategyId
        from tyrex_pm.runtime.startup_controller import StartupController

        decision = await request_decision(
            assessment,
            live=True,
            reader=self.decision_reader or _tty_decision_reader,
            timeout_s=self.config.startup.prompt_timeout_s,
        )
        proposal = proposal_for(assessment, decision.issue_id)
        if not decision.approved or proposal is None:
            return assessment
        controller = StartupController(
            coordinator=self.coordinator,
            generation=self.run_instance_id,
        )
        book_provider = getattr(self.gateway, "recovery_book", None)
        if not callable(book_provider):
            return assessment
        if not await controller.authorize(proposal, approved=True, live=True):
            return assessment

        async def _abort() -> Any:
            await controller.finish(proposal, "aborted")
            return assessment

        fresh = await self._read_startup_facts()
        observed = _recovery_observation(fresh, proposal)
        if observed is None:
            return await _abort()
        observed_shares, lifecycle_status = observed
        reason = controller.refresh_still_valid(
            proposal,
            shares=observed_shares,
            condition_id=proposal.condition_id,
            token_id=proposal.token_id,
            lifecycle_status=lifecycle_status,
        )
        if reason is not None:
            return await _abort()
        if observed_shares == 0 and proposal.action != "cancel":
            return await _abort()
        try:
            book = await book_provider(proposal.condition_id, proposal.token_id)
        except Exception:
            return await _abort()
        if book is None:
            return await _abort()
        self._recovery_book = lambda: book
        self._recovery_binding_id = book.binding_id
        saved_reads = self.capabilities.account_reads_ready
        saved_market = self.capabilities.active_market_ready
        saved_sellable = self.capabilities.selected_token_sellable
        try:
            self.coordinator.recovery_now = (
                (lambda: datetime.now(timezone.utc))
                if self.time_authority is None
                else self.time_authority.now_corrected_utc
            )
            if self.lifecycle.session_id is None:
                self.lifecycle.resume(proposal.session_id)
            self.capabilities.account_reads_ready = True
            self.capabilities.active_market_ready = True
            self.capabilities.selected_token_sellable = True
            intent = FlattenIntent(
                intent_id=new_intent_id(),
                strategy_id=StrategyId(self.config.strategy_kind),
                instrument_id=InstrumentId(proposal.token_id),
                market_id=MarketId(proposal.condition_id),
                created_at=datetime.now(timezone.utc),
                correlation_id=CorrelationId(proposal.issue_id),
                causation_id=None,
                reason_code="APPROVED_RECOVERY",
            )
            legs = (getattr(book, "up", None), getattr(book, "down", None))
            leg = next(
                (
                    candidate
                    for candidate in legs
                    if getattr(candidate, "token_id", None) == proposal.token_id
                ),
                None,
            )
            min_size = None if leg is None else getattr(leg, "min_order_size", None)
            bid = None if leg is None else getattr(getattr(leg, "quote", None), "best_bid", None)
            if proposal.action != "cancel" and (
                min_size is None or bid is None or Decimal(str(bid)) < proposal.min_price
            ):
                return await _abort()
            await self.lifecycle.submit_exit(
                intent,
                token_id=proposal.token_id,
            min_order_size=None if min_size is None else Decimal(str(min_size)),
            last_bid=None if bid is None else Decimal(str(bid)),
                historical_recovery=True,
            )
            await controller.finish(proposal, "submitted")
            state = self.lifecycle.state
            self.recovery_sessions.append(
                {
                    "session_id": proposal.session_id,
                    "condition_id": proposal.condition_id,
                    "action": proposal.action,
                    "max_shares": str(proposal.max_shares),
                    "phase": None if state is None else state.phase.value,
                }
            )
            if state is not None and state.safe_to_open_entry:
                self.lifecycle.release_terminal()
        finally:
            self.capabilities.account_reads_ready = saved_reads
            self.capabilities.active_market_ready = saved_market
            self.capabilities.selected_token_sellable = saved_sellable
            self._recovery_book = None
            self._recovery_binding_id = None
        return await self._assess_startup()

    async def run(self) -> TradingRunResult:
        try:
            return await self._run_body()
        finally:
            await self._stop_startup_monitor()
            self._release_state_lock()

    async def _stop_startup_monitor(self) -> None:
        task = self._startup_monitor
        self._startup_monitor = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run_body(self) -> TradingRunResult:
        assessment = await self._assess_startup()
        if self.capabilities.live_requested and any(
            issue.code == "LEGACY_JOURNAL_UNBOUND" for issue in assessment.issues
        ):
            from tyrex_pm.adapters.polymarket.startup_reads import associate_legacy_journal
            from tyrex_pm.application.startup_interaction import request_legacy_association

            accepted = await request_legacy_association(
                live=True,
                reader=self.decision_reader or _tty_decision_reader,
                timeout_s=self.config.startup.prompt_timeout_s,
                wallet_display=assessment.wallet_display,
            )
            if accepted and self._startup_wallet:
                associate_legacy_journal(
                    self.coordinator.journal,
                    self._startup_wallet,
                    at=(
                        datetime.now(timezone.utc)
                        if self.time_authority is None
                        else self.time_authority.now_corrected_utc()
                    ),
                )
                assessment = await self._assess_startup()
        if (
            assessment.status.value == "ACTION_REQUIRED"
            and self.capabilities.live_requested
        ):
            assessment = await self._resolve_startup_action(assessment)
        startup_ready = (
            assessment.ready and self._last_inspection_publication == "accepted"
        )
        if startup_ready:
            self._mark_startup_fresh()
            self._startup_monitor = asyncio.create_task(
                self._startup_monitor_loop(), name="startup-readiness"
            )
        else:
            self.startup_outcome = (
                "STARTUP_ACTION_REQUIRED"
                if assessment.status.value == "ACTION_REQUIRED"
                else "STARTUP_RECONCILIATION_BLOCKED"
            )
        runtime = MarketSessionRuntime.create(
            clock=SystemClock(),
            strategy_kind=self.config.strategy_kind,
            strategy_config=self.config.strategy,
            require_ssr_price_match=self.config.market.require_ssr_price_match,
            target_notional=self.config.risk.target_notional,
            time_authority=self.time_authority,
            maximum_trade_age_ms=self.config.market.maximum_book_age_ms,
        )
        self.indicator_graph = runtime.indicator_graph
        self._market_runtime_ref = runtime
        self.settlement_worker = self._build_settlement_worker()
        self._open_recording()
        fatal_exception: BaseException | None = None
        fatal_error: dict[str, Any] | None = None
        reporting_failures: list[str] = []
        run_evidence: tuple[RunEvidenceRecord, ...] = ()
        if startup_ready:
            self._start_intent_dispatcher()
        from tyrex_pm.runtime.feed_diagnostics import EventLoopLagMonitor

        self.loop_lag_monitor = EventLoopLagMonitor(on_report=self.run_recorder.record)
        self.loop_lag_monitor.start()
        try:
            if startup_ready:
                self._strategy_started = True
                self.market_summary = await run_market_data_runtime(
                out_dir=self.output_directory,
                run_id=self.config.run_name,
                min_seals=999,
                max_duration_s=self._max_duration_s(),
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
                target_notional=self.config.risk.target_notional,
                before_promote=self.can_promote,
                on_feed_supervisor=self.on_feed_supervisor,
                on_async_tick=self.on_async_tick,
                evaluation_ready=self._entry_evaluation_ready,
                readiness_diagnostics=self.readiness_dict,
                on_runtime_error=self._record_runtime_error,
                max_clock_uncertainty_ms=int(max_clock_uncertainty_ms(self.config)),
                market_family=self.config.market.family,
                on_public_fact=self.on_public_fact,
                on_shutdown=self._shutdown_execution,
                on_feed_event=self.run_recorder.record,
                raw_capture=getattr(self.decision_recording, "raw", None),
            )
        except BaseException as exc:  # always project a report before propagating interrupts
            fatal_exception = exc
            fatal_error = {
                "stage": "market_data_runtime",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
            self._record_runtime_error("market_data_runtime", exc)
        finally:
            await self._stop_intent_dispatcher()
            if getattr(self, "decision_recording", None) is not None:
                self._close_recording()
            monitor = getattr(self, "loop_lag_monitor", None)
            if monitor is not None:
                await monitor.stop()
            warm_tasks = tuple(self._order_metadata_warm_tasks)
            for task in warm_tasks:
                task.cancel()
            if warm_tasks:
                await asyncio.gather(*warm_tasks, return_exceptions=True)
            self._order_metadata_warm_tasks.clear()

        state = self.lifecycle.state
        run_session_ids = list(getattr(self.lifecycle, "run_sessions", ()))
        try:
            if self.continuous and run_session_ids:
                # Every attempt of every window, including detached committed holds.
                mutation_attempts = sum(
                    self.coordinator.mutation_attempt_count(sid) for sid in run_session_ids
                )
            else:
                mutation_attempts = (
                    0
                    if state is None
                    else self.coordinator.mutation_attempt_count(state.session_id)
                )
            self._process_mutation_attempts = self.coordinator.mutation_attempt_count(None)
        except Exception as exc:
            mutation_attempts = None
            self._process_mutation_attempts = None
            reporting_failures.append(f"mutation_projection:{type(exc).__name__}:{exc}")
        self._run_session_states = (
            tuple(
                self.coordinator.states[sid]
                for sid in run_session_ids
                if sid in self.coordinator.states
            )
            if self.continuous
            else ()
        )
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
        self._release_state_lock()
        if isinstance(fatal_exception, (KeyboardInterrupt, asyncio.CancelledError)):
            raise fatal_exception
        return result

    def _max_duration_s(self) -> float | None:
        market = self.config.market
        if getattr(market, "run_mode", "single_window") == "continuous":
            # Until stopped, unless an explicit bounded soak length was configured.
            return market.maximum_duration_s if market.maximum_duration_explicit else None
        return market.maximum_duration_s

    def _build_settlement_worker(self) -> Any:
        """Automatic resolution/payout tracking for continuous runs (None otherwise)."""
        hooks = self.settlement_hooks
        settlement = getattr(self.config, "settlement", None)
        if hooks is None or settlement is None or not self.continuous:
            return None
        from tyrex_pm.execution.settlement.resolution import ResolutionManager
        from tyrex_pm.runtime.settlement_worker import SettlementWorker

        reader = getattr(self.gateway, "payout_reader", None)
        chain_reader = None
        if reader is None:
            chain_reader = _chain_reader(self.polygon_rpc_url)
            reader = _chain_payout_reader(self.polygon_rpc_url, chain_reader)
        if reader is None:
            self.run_recorder.record(
                "SETTLEMENT_WORKER_UNAVAILABLE",
                {"reason": "no Polygon RPC configured (TYREX_POLYGON_RPC_URL)",
                 "effect": "holdings stay unresolved; unresolved caps pause entries"},
            )
            return None
        paper = getattr(self.config, "paper", None)
        verifier = getattr(self.gateway, "verify_redemption", None)
        wallet = getattr(self.gateway, "wallet", None) if paper is not None else (
            settlement.redemption_wallet
        )
        manager = ResolutionManager(
            hooks.ledger,
            payout_reader=reader,
            redemption_verifier=verifier if paper is not None else None,
            wallet=wallet,
        )
        redeemer = None
        if paper is not None and paper.auto_redeem_after_s is not None:
            from tyrex_pm.execution.paper import delayed_paper_redeemer

            redeemer = delayed_paper_redeemer(self.gateway, paper.auto_redeem_after_s)
        discovery = None
        options = getattr(settlement, "redemption_discovery", None)
        if (paper is None and chain_reader is not None and wallet
                and options is not None and options.enabled):
            # The trading process owns the ledger while it runs (state lock held):
            # discovery runs in this worker, outside evaluation and order submission.
            from tyrex_pm.execution.settlement.redemption_discovery import (
                DiscoveryPolicy,
                RedemptionDiscovery,
            )

            discovery = RedemptionDiscovery(
                manager, chain_reader, wallet=wallet,
                state_path=settlement.ledger_path.parent / "redemption_discovery.json",
                policy=DiscoveryPolicy(
                    interval_s=options.interval_s,
                    max_blocks_per_pass=options.max_blocks_per_pass,
                    max_log_calls_per_pass=options.max_log_calls_per_pass),
                record=self.run_recorder.record)
        return SettlementWorker(
            manager=manager,
            interval_s=settlement.resolution_poll_interval_s,
            record=self.run_recorder.record,
            paper_redeemer=redeemer,
            discovery=discovery,
        )

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
                    _merge_prepare_timings(row, event)
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
                    _merge_prepare_timings(row, event)
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
            live_requested=self.capabilities.live_requested,
            state=state,
            mutation_attempts=mutation_attempts,
            process_mutation_attempts=self._process_mutation_attempts,
            strategy_started=self._strategy_started,
            execution_timeline=self.execution_timeline,
            capabilities=capabilities,
            market_data=market_data,
            run_evidence=run_evidence,
            runtime_errors=self.runtime_errors,
            fatal_error=fatal_error,
            reporting_failures=reporting_failures,
            prior_session_risk=self.prior_session_risk,
            reproducibility=self.reproducibility,
            startup_outcome=self.startup_outcome,
            startup_reconciliation=self.startup_report,
            recovery_sessions=tuple(self.recovery_sessions),
            run_sessions=getattr(self, "_run_session_states", ()),
            committed_session_ids=frozenset(
                getattr(self.lifecycle, "detached_sessions", ())
            ),
            recording=self.recording_status,
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
                reproducibility=self.reproducibility,
            )
        return TradingRunResult(
            outcome=str(payload["outcome"]),
            ok=bool(payload["ok"]),
            report_path=path,
            mutation_attempts=mutation_attempts,
            state=state,
        )


RECORDING_STATUS_INTERVAL_S = 60.0
# Decision-row fields kept in the STRATEGY_DECISION journal event when the full row is
# in the decision recording (watch-run and the run report read these).
_JOURNAL_DECISION_FIELDS = (
    "tau_s", "p_up_point", "up_ask", "up_bid", "down_ask", "down_bid", "chosen_side",
    "episode_seq", "attempt_seq", "episode_phase", "disagreement_gap_pp",
)


def _compact_decision_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    row = evidence.get("decision_row")
    if not isinstance(row, dict):
        return evidence
    return {**evidence, "decision_row": {k: row[k] for k in _JOURNAL_DECISION_FIELDS
                                         if k in row}, "decision_row_compact": True}


def _recording_status_event(status: dict[str, Any]) -> dict[str, Any]:
    """Compact status for the run journal (counters, no gap lists)."""
    streams = {}
    for name, snap in (status.get("streams") or {}).items():
        streams[name] = {key: value for key, value in snap.items() if key != "gaps"}
        streams[name]["gap_count"] = len(snap.get("gaps") or ())
    return {
        "failed": status.get("failed"), "dropped": status.get("dropped"),
        "writer_alive": status.get("writer_alive"),
        "writer_cpu_s": status.get("writer_cpu_s"),
        "capture_cost_us": status.get("capture_cost_us"),
        "capture_errors": status.get("capture_errors"),
        "undeclared_fields": status.get("undeclared_fields"),
        "streams": streams, "run_evidence": status.get("run_evidence"),
        "error": status.get("error"),
    }


# Hold refusals that clear by themselves (trades confirming, a cancel being proven).
_TRANSIENT_HOLD_REFUSALS = frozenset(
    {"NO_CONFIRMED_INVENTORY", "INVENTORY_DIFFERS_FROM_LEDGER",
     "CONFLICTING_ORDER_NOT_PROVEN_CANCELLED", "UNRESOLVED_MUTATION"}
)

def _chain_reader(url: str | None = None):
    """Read-only Polygon settlement reader for ``url`` (TYREX_POLYGON_RPC_URL), or None."""
    try:
        from tyrex_pm.adapters.polymarket.polygon_rpc import POLYGON_RPC_ENV
        from tyrex_pm.execution.settlement.chain import SettlementChainReader
    except Exception:  # noqa: BLE001 - optional live dependency
        return None
    url = url or os.environ.get(POLYGON_RPC_ENV, "")
    if not url:
        return None
    return SettlementChainReader(url)


def _chain_payout_reader(url: str | None = None, reader: Any = None):
    """Read-only finalized payout reader for ``url`` (TYREX_POLYGON_RPC_URL), or None."""
    reader = reader or _chain_reader(url)
    if reader is None:
        return None
    from tyrex_pm.execution.settlement.chain import finalized_payout_reader

    return finalized_payout_reader(reader)


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


def build_paper_venue(config: TradingRunConfig) -> Any:
    """Paper composition: simulated account/venue in the paper state directory.

    Never constructs the authenticated gateway or reads trading credentials.
    """
    from tyrex_pm.execution.paper import PaperAccount, PaperVenue
    from tyrex_pm.strategies.registry import get_strategy_plugin

    paper = config.paper
    if paper is None:
        raise ValueError("venue: paper requires a paper block")
    account = PaperAccount.open(
        config.state_directory / "paper_account.json",
        initial_collateral=paper.initial_collateral,
    )
    plugin = get_strategy_plugin(config.strategy_kind)
    driver = plugin.create_driver(config=config.strategy, clock=SystemClock())
    curve = getattr(driver, "fee_curve", None)
    if curve is None:
        raise ValueError("paper venue needs the strategy's fee curve")
    return PaperVenue(
        account=account,
        fee_curve=lambda _token_id: curve,
        order_latency_ms=paper.order_latency_ms,
        participation=paper.participation,
        wallet=f"paper:{config.run_name}",
    )


async def run_trading_runtime(
    *,
    config: TradingRunConfig,
    output_directory: Path,
    dotenv: Path | None,
    config_path: Path | str | None = None,
    gateway: Any | None = None,
) -> TradingRunResult:
    from tyrex_pm.reporting.reproducibility import capture_reproducibility

    run_instance_id = str(uuid4())
    output_directory.mkdir(parents=True, exist_ok=True)
    reproducibility = capture_reproducibility(
        config=config,
        config_path=config_path,
        cli_flags={"live": True, "out_dir": output_directory},
        run_instance_id=run_instance_id,
    )
    try:
        paper = getattr(config, "venue", "live") == "paper"
        if paper and gateway is None:
            gateway = build_paper_venue(config)
        runtime = await TradingRuntime.create(
            config=config,
            output_directory=output_directory,
            # Paper runs never read credentials: the environment is not passed on.
            env={} if paper else load_dotenv_values(dotenv),
            gateway=gateway,
            run_instance_id=run_instance_id,
        )
        if paper:
            runtime.gateway.book_provider = runtime.final_gate._capture_book
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
            reproducibility=reproducibility,
        )
        return TradingRunResult(
            outcome=str(payload["outcome"]),
            ok=False,
            report_path=path,
            mutation_attempts=0,
            state=None,
        )
    runtime.reproducibility = reproducibility
    return await runtime.run()


def _recovery_observation(facts: Any, proposal: Any):
    """Shares and lifecycle from a fresh read. None means the recovery must stop."""
    from tyrex_pm.execution.startup_reconciliation import _holdings_changed, _orders_changed

    if facts is None or facts.identity is None or not facts.coverage.complete:
        return None
    if _holdings_changed(facts.holdings, facts.closing_holdings):
        return None
    if _orders_changed(facts.open_orders, facts.closing_open_orders):
        return None
    shares = Decimal("0")
    for holding in facts.holdings:
        if holding.shares <= 0 or holding.token_id != proposal.token_id:
            continue
        if holding.condition_id != proposal.condition_id:
            return None
        shares += holding.shares
    allowed_orders = set(proposal.venue_order_ids)
    for order in facts.open_orders:
        if order.token_id != proposal.token_id or str(order.side).upper() != "BUY":
            continue
        if order.venue_order_id not in allowed_orders:
            return None
    lifecycle = facts.lifecycles.get(proposal.condition_id)
    status = "" if lifecycle is None else lifecycle.status
    return shares, status


async def _tty_decision_reader(prompt: str) -> str | None:
    """Read one operator line. A non-TTY or EOF declines; it never approves."""
    import sys

    if sys.stdin is None or not sys.stdin.isatty():
        return None
    try:
        return await asyncio.to_thread(input, prompt)
    except EOFError:
        return None


async def run_account_inspection(
    *,
    config: TradingRunConfig,
    output_directory: Path,
    dotenv: Path | None,
) -> str:
    """Same classification as startup, with signing and venue mutations disabled."""
    output_directory.mkdir(parents=True, exist_ok=True)
    gateway = await PolymarketAsyncGateway.from_env(env=load_dotenv_values(dotenv))
    gateway.mutations_enabled = False
    runtime = await TradingRuntime.create(
        config=config,
        output_directory=output_directory,
        env={},
        gateway=gateway,
        live_requested=False,
    )
    try:
        assessment = await runtime._assess_startup()
        return assessment.status.value
    finally:
        await runtime.coordinator.close()
        await gateway.close()
        runtime._release_state_lock()
