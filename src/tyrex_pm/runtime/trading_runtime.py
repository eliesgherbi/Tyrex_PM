"""The single production orchestration path from market data to terminal state."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.time_authority import TimeAuthority
from tyrex_pm.execution.account_state import (
    AccountStateAuthority,
    AccountStateSnapshot,
)
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator
from tyrex_pm.execution.final_gate import FinalExecutionGate
from tyrex_pm.execution.lifecycle import ExecutionLifecycle
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.session_state import (
    ExecutionSessionState,
    KnownDustResidual,
)
from tyrex_pm.persistence.run_evidence_journal import (
    RunEvidenceRecord,
    RunEvidenceRecorder,
)
from tyrex_pm.protection import (
    ProtectionRegistry,
)
from tyrex_pm.reporting.run_report import (
    write_emergency_report,
)
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.host.composition import CompositionMixin
from tyrex_pm.runtime.host.deadlines import DeadlineMixin
from tyrex_pm.runtime.host.decisions import DecisionMixin
from tyrex_pm.runtime.host.intents import IntentExecutionMixin
from tyrex_pm.runtime.host.metadata import OrderMetadataMixin
from tyrex_pm.runtime.host.protection import ProtectionMixin
from tyrex_pm.runtime.host.readiness import ReadinessMixin
from tyrex_pm.runtime.host.reporting import ReportingMixin
from tyrex_pm.runtime.host.settlement_lifecycle import SettlementLifecycleMixin
from tyrex_pm.runtime.host.shared import (
    TradingRunResult,
    _hold_committed,
    _tty_decision_reader,
)
from tyrex_pm.runtime.host.startup import StartupMixin
from tyrex_pm.runtime.host.window_binding import WindowBindingMixin
from tyrex_pm.runtime.market_data_runtime import MarketDataSummary, run_market_data_runtime
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime
from tyrex_pm.runtime.run_config import (
    TradingRunConfig,
    max_clock_uncertainty_ms,
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














@dataclass
class TradingRuntime(
    CompositionMixin,
    OrderMetadataMixin,
    ReadinessMixin,
    WindowBindingMixin,
    SettlementLifecycleMixin,
    DecisionMixin,
    IntentExecutionMixin,
    ProtectionMixin,
    DeadlineMixin,
    StartupMixin,
    ReportingMixin,
):
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
    _ledger_writer: Any = None
    _trading_evaluation_seq: int = 0
    _capture_seeded_windows: dict = field(default_factory=dict)
    _captured_account_refs: dict = field(default_factory=dict)
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
    # Set by the host components while running (declared so every reader sees one default).
    # Restored hold whose original lifecycle policy is missing or disagrees: no new exposure.
    _recovery_policy_unresolved: bool = False
    # Window whose hold commit was refused transiently and is retried on the next tick.
    _pending_hold_commit: str | None = None
    # Last (window, detail) recorded for a refused hold commit / failed detach (no repeats).
    _hold_commit_noted: tuple[str, str] | None = None
    _detach_noted: tuple[str, str] | None = None
    # Entry intent id -> monotonic ns at submission, for entry latency.
    _entry_submit_started_ns: dict[str, int] = field(default_factory=dict)
    # Execution sessions of this run, frozen at shutdown for the report.
    _run_session_states: tuple[Any, ...] = ()
    loop_lag_monitor: Any | None = None

    def on_feed_supervisor(self, supervisor: Any) -> None:
        self.feed_supervisor = supervisor
        if getattr(supervisor, "store", None) is not None:
            supervisor.store.rest_applied_sink = self._capture_rest_book
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














    async def _shutdown_execution(self, reason: str) -> None:
        async with self._cleanup_lock:
            if self._cleanup_done:
                return
            self._cleanup_done = True
            self.stop_requested = True
            self.capabilities.entry_window_open = False
            self.capabilities.set_blocker("STOP_REQUESTED", reason)
            await self._stop_intent_dispatcher(cancel_inflight=False)
            worker = self.settlement_worker
            if worker is not None:
                await worker.stop()
            summarize = self._summarize_window
            active = self.active_market
            if summarize is not None and active is not None:
                summarize(active.window_id, f"stop:{reason}")
            if _hold_committed(self):
                await self._cancel_for_hold()
                self.run_recorder.record("SHUTDOWN_HOLD_PRESERVED", {"reason": reason})
                return
            residual = self._shutdown_residual_hold
            if residual is not None and await residual(reason):
                return
            if await self._preserve_uncommitted_hold(reason):
                return
            await self.lifecycle.drain_open_session(
                reason,
                min_order_size=self._held_min_order_size(),
                last_bid=self._held_best_bid(self.active_market),
            )




















    # -- decision recording (recording.host) ---------------------------------------























    # -- episode / attempt plumbing ----------------------------------------------------


























    def _release_state_lock(self) -> None:
        writer = self._ledger_writer
        self._ledger_writer = None
        if writer is not None:
            writer.release()
        lock = self._state_lock
        self._state_lock = None
        if lock is not None:
            lock.release()





    def _corrected_now(self) -> datetime:
        authority = self.time_authority
        if authority is None:
            return datetime.now(timezone.utc)
        return authority.now_corrected_utc()













    async def run(self) -> TradingRunResult:
        try:
            return await self._run_body()
        finally:
            await self._stop_startup_monitor()
            self._release_state_lock()


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
            if self.decision_recording is not None:
                self._close_recording()
            monitor = self.loop_lag_monitor
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
    curve = driver.fee_curve
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
    paper = getattr(config, "venue", "live") == "paper"
    reproducibility = capture_reproducibility(
        config=config,
        config_path=config_path,
        cli_flags={"live": not paper, "paper": paper, "out_dir": output_directory},
        run_instance_id=run_instance_id,
    )
    try:
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
            venue=getattr(config, "venue", None),
            real_orders_authorized=not paper,
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
