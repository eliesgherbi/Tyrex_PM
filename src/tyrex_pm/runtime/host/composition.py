"""Composition of a trading runtime: locks, venue gateway, gate, journal, coordinator, lifecycle."""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from tyrex_pm.core.clock import SystemClock
from tyrex_pm.core.time_authority import SnapshotTimeAuthority
from tyrex_pm.execution.account_state import (
    AccountStateAuthority,
    AccountStatePolicy,
)
from tyrex_pm.execution.coordinator import AccountExecutionCoordinator
from tyrex_pm.execution.final_gate import FinalExecutionGate, FinalGatePolicy
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.polymarket.gateway import PolymarketAsyncGateway
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import (
    ExecutionSessionState,
)
from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal
from tyrex_pm.persistence.run_evidence_journal import (
    RunEvidenceRecorder,
    SqliteRunEvidenceJournal,
)
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.run_config import (
    TradingRunConfig,
    effective_entry_cutoff_s,
    max_clock_uncertainty_ms,
)
from tyrex_pm.runtime.settlement_hooks import SettlementHooks

if TYPE_CHECKING:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime


class CompositionMixin:
    """Builds a ``TradingRuntime`` from its run configuration (``create``).

    Mixed into ``TradingRuntime``; ``cls`` is the runtime class.
    """

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
        from tyrex_pm.execution.process_lock import LedgerWriter, StateDirectoryLock

        state_lock = StateDirectoryLock(config.state_directory)
        state_lock.acquire()
        ledger_writer = None
        try:
            if config.settlement is not None:
                ledger_writer = LedgerWriter(config.settlement.ledger_path)
                ledger_writer.acquire()
            return await cls._create_locked(
                config=config,
                output_directory=output_directory,
                env=env,
                gateway=gateway,
                run_instance_id=run_instance_id,
                live_requested=live_requested,
                state_lock=state_lock,
                ledger_writer=ledger_writer,
            )
        except BaseException:
            if ledger_writer is not None:
                ledger_writer.release()
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
        ledger_writer: Any = None,
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
        result.settlement_hooks = SettlementHooks.from_config(config, ledger_writer=ledger_writer)
        result._ledger_writer = ledger_writer
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
        final_gate.strategy_sell_check = result._value_exit_check
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
