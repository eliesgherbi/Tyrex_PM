"""F17 / F-INT shutdown tests through the market-data finally path."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from support.runtime import bare_runtime

from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId, TokenId
from tyrex_pm.core.instruments import Instrument, OutcomeSide
from tyrex_pm.core.intents import EnterIntent, new_intent_id
from tyrex_pm.domain.polymarket.discovery_binding import (
    DiscoveredMarketBinding,
    DiscoverySessionRole,
)
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.outcome_map import NormalizedLeg, OutcomeLegBinding, OutcomeMap
from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
    AccountStateSnapshot,
    TokenAccountState,
)
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import SubmissionFailed, SubmissionResponseObserved
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.protection.registry import ProtectionRegistry
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.market_data_runtime import run_market_data_runtime
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime
from tyrex_pm.runtime.run_config import load_trading_run_config
from tyrex_pm.runtime.trading_runtime import TradingRuntime

NOW = datetime(2026, 9, 20, 18, 0, tzinfo=timezone.utc)


def _market(slug: str = "btc-updown-5m-1") -> BinaryMarket:
    yes = Instrument(
        market_id=MarketId(slug),
        token_id=TokenId("yes"),
        instrument_id=InstrumentId("yes"),
        outcome=OutcomeSide.YES,
    )
    no = Instrument(
        market_id=MarketId(slug),
        token_id=TokenId("no"),
        instrument_id=InstrumentId("no"),
        outcome=OutcomeSide.NO,
    )
    return BinaryMarket(
        market_id=MarketId(slug),
        condition_id=f"{slug}-condition",
        question="shutdown test",
        yes=yes,
        no=no,
        event_start=NOW,
        event_end=NOW + timedelta(minutes=5),
    )


def _binding(slug: str, role: DiscoverySessionRole) -> DiscoveredMarketBinding:
    market = _market(slug)
    outcomes = OutcomeMap(
        legs=(
            OutcomeLegBinding(
                leg=NormalizedLeg.UP, venue_label="Up", token_id=TokenId("yes"), outcome_index=0
            ),
            OutcomeLegBinding(
                leg=NormalizedLeg.DOWN, venue_label="Down", token_id=TokenId("no"), outcome_index=1
            ),
        )
    )
    return DiscoveredMarketBinding(
        market=market,
        outcomes=outcomes,
        session_role=role,
        window_slug=slug,
        resolution_source="test",
        resolution_rule_fingerprint="fp",
        market_rule_ok=True,
    )


class _HangVenue:
    def __init__(self, hang: str) -> None:
        self.hang = hang
        self.posts: list[str] = []
        self.prepares = 0
        self.closed = False
        self.prepare_entered = asyncio.Event()
        self.post_entered = asyncio.Event()
        self.release = asyncio.Event()

    async def prepare_order(self, spec):  # noqa: ANN001
        self.prepares += 1
        self.prepare_entered.set()
        if self.hang == "prepare":
            await self.release.wait()
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def post_order(self, prepared):  # noqa: ANN001
        self.post_entered.set()
        if self.hang == "post":
            await self.release.wait()
        self.posts.append(prepared.payload.side.value)
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id="v1",
            status="unmatched",
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        return cancel_result_from_sdk({"canceled": tuple(order_ids), "not_canceled": {}})

    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **_kwargs):
        return ()

    async def conditional_balance(self, _token_id):
        return Decimal("0"), Decimal("100")

    async def close(self):
        self.closed = True


def _lifecycle(venue: _HangVenue) -> ExecutionLifecycle:
    coordinator = AccountExecutionCoordinator(
        journal=MemoryExecutionJournal(),
        gateway=venue,
        final_gate=lambda spec: _gate(spec),
    )
    return ExecutionLifecycle(
        coordinator=coordinator,
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=CapabilityController(
            live_requested=True,
            public_feeds_ready=True,
            active_market_ready=True,
            books_ready=True,
            model_ready=True,
            account_reads_ready=True,
            user_stream_ready=True,
            collateral_ready=True,
            entry_allowance_ready=True,
            order_metadata_ready=True,
            prior_scope_clear=True,
            entry_window_open=True,
            selected_token_sellable=True,
        ),
        policy=LifecyclePolicy(
            evidence_timeout_s=0.2,
            reconciliation_interval_s=0.001,
            exit_retry_limit=1,
            exit_retry_budget_s=0.2,
            cancel_delay_s=0.001,
        ),
    )


async def _gate(spec):  # noqa: ANN001
    from tyrex_pm.execution.orders import MarketBuyOrderSpec

    runtime = getattr(_gate, "runtime", None)
    if runtime is not None and runtime.capabilities.snapshot(exposed=False).blockers:
        if "STOP_REQUESTED" in runtime.capabilities.snapshot(exposed=False).blockers:
            return False, "entry_capability_blocked:STOP_REQUESTED"
    if isinstance(spec, MarketBuyOrderSpec) and runtime is not None:
        snap = runtime.capabilities.snapshot(exposed=False)
        if not snap.entry_executable:
            return False, "entry_capability_blocked:" + ",".join(snap.blockers)
    return True, None


class _ReadyAccountAuthority:
    def __init__(self, snapshot: AccountStateSnapshot) -> None:
        self._snapshot = snapshot

    def current(self, _market_id: str) -> AccountStateSnapshot:
        return self._snapshot

    @asynccontextmanager
    async def hold_ready_snapshot(self, _market_id: str):
        yield


def _dispatch_runtime(lifecycle: ExecutionLifecycle) -> TradingRuntime:
    market = _market()
    snapshot = AccountStateSnapshot(
        market_id=market.condition_id,
        token_ids=("yes", "no"),
        status=AccountSnapshotStatus.READY,
        observed_at=NOW,
        observed_monotonic_s=0.0,
        collateral_balance=Decimal("20"),
        collateral_allowance=Decimal("20"),
        tokens=(
            TokenAccountState("yes", Decimal("0"), Decimal("100")),
            TokenAccountState("no", Decimal("0"), Decimal("100")),
        ),
        open_order_ids=(),
        blockers=(),
        read_attempt=1,
    )
    runtime = bare_runtime()
    runtime.lifecycle = lifecycle
    runtime.capabilities = lifecycle.capabilities
    runtime.config = load_trading_run_config(Path("config/runs/z_gap_tiny_live.yaml"))
    runtime.account_state_authority = _ReadyAccountAuthority(snapshot)
    runtime.account_state = snapshot
    runtime.active_market = SimpleNamespace(market=market, window_id="w")
    runtime.stream_session_id = "session"
    runtime.time_authority = SimpleNamespace(
        now_corrected_utc=lambda: NOW + timedelta(seconds=30)
    )
    runtime.feed_supervisor = None
    runtime.selected_instrument = None
    runtime.selected_token_id = None
    runtime.intent_queue = asyncio.Queue()
    runtime.intent_wake = asyncio.Event()
    runtime.intent_candidate_monotonic_ns = {}
    runtime.seen_intents = set()
    runtime._intent_dispatcher_task = None
    runtime._intent_dispatcher_stop = asyncio.Event()
    runtime._cleanup_done = False
    runtime._cleanup_lock = asyncio.Lock()
    runtime.stop_requested = False
    runtime.run_recorder = SimpleNamespace(record=lambda *_a, **_k: None)
    runtime._last_readiness_key = None
    runtime.known_dust_residuals = []
    runtime.protection_registry = ProtectionRegistry()
    runtime.restored_sessions = []
    runtime.runtime_errors = []
    _gate.runtime = runtime
    return runtime


def _entry() -> EnterIntent:
    return EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("ask70"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("m"),
        created_at=NOW,
        correlation_id=CorrelationId("c"),
        causation_id=None,
        reason_code="ENTRY_CANDIDATE",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.70"),
    )


async def _drive_finally(tmp_path, on_shutdown, should_stop, on_async_tick=None, monkeypatch=None):
    order = []
    runtime = MarketSessionRuntime.create(
        strategy_kind="z_gap",
        require_ssr_price_match=False,
    )

    class _Family:
        async def resolve_active(self, _discovery=None):
            return _binding("btc-updown-5m-1", DiscoverySessionRole.ACTIVE)

        async def prepare_next(self, _discovery=None):
            return _binding("btc-updown-5m-2", DiscoverySessionRole.PREPARED_NEXT)

    async def idle(*_a, **_k):
        while True:
            await asyncio.sleep(60)

    async def noop_set(self, rec):  # noqa: ANN001
        self.active = SimpleNamespace(
            binding=rec,
            phase=SimpleNamespace(value="READY"),
            connection_health=SimpleNamespace(value="LIVE"),
        )
        if not hasattr(self, "prepared"):
            self.prepared = None

    async def record_stop(self):  # noqa: ANN001
        order.append("feed_stop")

    if monkeypatch is not None:
        monkeypatch.setattr(
            "tyrex_pm.runtime.market_data_runtime.get_market_family",
            lambda _name: _Family(),
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.clock_sync.OsMonitorClockSyncProvider.measure",
            AsyncMock(side_effect=RuntimeError("no clock")),
        )
        monkeypatch.setattr(
            "tyrex_pm.market_data.book_feed.BookFeedSupervisor.set_active",
            noop_set,
        )
        monkeypatch.setattr(
            "tyrex_pm.market_data.book_feed.BookFeedSupervisor.set_prepared",
            noop_set,
        )
        monkeypatch.setattr(
            "tyrex_pm.market_data.book_feed.BookFeedSupervisor.stop",
            record_stop,
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.clock_sync.ClockSyncLoop.run",
            idle,
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.polymarket.rtds_adapter.RtdsChainlinkAdapter.run",
            idle,
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.binance.ws_adapter.BinanceTradeWsAdapter.run",
            idle,
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.binance.depth_adapter.BinanceDepthWsAdapter.run",
            idle,
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.clock_sync.ClockSyncLoop.stop",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.polymarket.rtds_adapter.RtdsChainlinkAdapter.stop",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.binance.ws_adapter.BinanceTradeWsAdapter.stop",
            AsyncMock(),
        )
        monkeypatch.setattr(
            "tyrex_pm.adapters.binance.depth_adapter.BinanceDepthWsAdapter.stop",
            AsyncMock(),
        )

    async def wrapped_shutdown(reason: str) -> None:
        order.append("drain")
        await on_shutdown(reason)

    await run_market_data_runtime(
        out_dir=tmp_path,
        run_id="shutdown",
        min_seals=999,
        max_duration_s=1,
        duration_anchor="compose_start",
        require_ssr_price_match=False,
        should_stop=should_stop,
        runtime=runtime,
        on_shutdown=wrapped_shutdown,
        on_async_tick=on_async_tick,
        market_family="btc_updown_5m",
    )
    return order


@pytest.mark.asyncio
async def test_f17_and_f_int_wait_drain_once_before_feeds(tmp_path, monkeypatch) -> None:
    drains = []

    async def shutdown(reason: str) -> None:
        drains.append(reason)

    order = await _drive_finally(
        tmp_path,
        shutdown,
        should_stop=lambda: True,
        monkeypatch=monkeypatch,
    )
    assert order.count("drain") == 1
    assert order.index("drain") < order.index("feed_stop")


@pytest.mark.asyncio
async def test_f_int_prepare_closes_buy_gate(tmp_path, monkeypatch) -> None:
    venue = _HangVenue("prepare")
    lifecycle = _lifecycle(venue)
    runtime = bare_runtime()
    runtime.lifecycle = lifecycle
    runtime.capabilities = lifecycle.capabilities
    runtime.stop_requested = False
    runtime._cleanup_done = False
    runtime._cleanup_lock = asyncio.Lock()
    runtime._intent_dispatcher_task = None
    runtime._intent_dispatcher_stop = asyncio.Event()
    _gate.runtime = runtime

    async def stop_dispatcher(*, cancel_inflight=True):
        return None

    runtime._stop_intent_dispatcher = stop_dispatcher
    await lifecycle.open(
        session_id="session",
        strategy_id="ask70",
        market_id="m",
        window_id="w",
        token_id="yes",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    task = asyncio.create_task(lifecycle.submit_entry(_entry(), token_id="yes"))
    await venue.prepare_entered.wait()

    async def on_tick(_runtime) -> None:
        await runtime._shutdown_execution("caller_requested")

    stopped = {"once": False}

    def should_stop() -> bool:
        if stopped["once"]:
            return True
        stopped["once"] = True
        return False

    await _drive_finally(
        tmp_path,
        runtime._shutdown_execution,
        should_stop=should_stop,
        on_async_tick=on_tick,
        monkeypatch=monkeypatch,
    )
    venue.release.set()
    state = await task
    assert venue.posts == []
    assert "STOP_REQUESTED" in runtime.capabilities.snapshot(exposed=False).blockers
    assert state.phase in {
        ExecutionPhase.COMPLETED_NO_DISPATCH,
        ExecutionPhase.MANUAL_INTERVENTION,
        ExecutionPhase.ENTRY_PREPARING,
    }
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_f_int_post_waits_and_does_not_start_second_buy(tmp_path, monkeypatch) -> None:
    venue = _HangVenue("post")
    lifecycle = _lifecycle(venue)
    runtime = _dispatch_runtime(lifecycle)
    runtime._start_intent_dispatcher()
    runtime._enqueue_intent(_entry())
    await asyncio.wait_for(venue.post_entered.wait(), timeout=2)
    drain_started = asyncio.Event()
    shutdown_done = asyncio.Event()

    async def on_tick(_runtime) -> None:
        drain_started.set()
        await runtime._shutdown_execution("caller_requested")
        shutdown_done.set()

    stopped = {"n": 0}

    def should_stop() -> bool:
        stopped["n"] += 1
        return stopped["n"] > 1

    drive = asyncio.create_task(
        _drive_finally(
            tmp_path,
            runtime._shutdown_execution,
            should_stop=should_stop,
            on_async_tick=on_tick,
            monkeypatch=monkeypatch,
        )
    )
    await drain_started.wait()
    await asyncio.sleep(0.05)
    assert not shutdown_done.is_set()
    assert not drive.done()
    assert not venue.closed
    events = lifecycle.coordinator.journal.load("session")
    assert events
    assert venue.posts == []
    venue.release.set()
    order = await asyncio.wait_for(drive, timeout=5)
    await asyncio.wait_for(shutdown_done.wait(), timeout=2)
    events = lifecycle.coordinator.journal.load("session")
    assert any(
        isinstance(event, (SubmissionResponseObserved, SubmissionFailed)) for event in events
    )
    assert venue.posts.count("BUY") == 1
    assert order.count("drain") == 1
    assert order.index("drain") < order.index("feed_stop")
    dispatcher = runtime._intent_dispatcher_task
    assert dispatcher is None or dispatcher.done()
    await lifecycle.coordinator.close()
