"""Production Z-Gap runtime wiring for single-session orchestrator."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import RunId
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_Z_GAP_RUNTIME_STARTED
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    AppConfig,
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.signal_feed_runtime import (
    SignalFeedRuntimeState,
    signal_feeds_requested,
    start_signal_feeds,
    stop_signal_feeds,
)
from tyrex_pm.runtime.time_authority import sample_offset
from tyrex_pm.runtime.z_gap_enforce import run_z_gap_enforce_loop
from tyrex_pm.runtime.z_gap_run import (
    ZGapStartupTimings,
    run_z_gap_observe_loop,
    sigma_config_from_zg,
)
from tyrex_pm.runtime.market_data_runtime import ensure_market_state_store
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.facts import ZGapObserveRuntimeState, handle_feed_health_callback
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator
from tyrex_pm.runtime.z_gap_sigma_warmup import SigmaWarmupState, warm_sigma_before_boundary

log = logging.getLogger(__name__)


@dataclass
class SessionRuntimeHandle:
    repo_root: Path
    runs_dir: Path
    run_id: RunId
    coord: RuntimeCoordinator
    sink: JsonlSink
    stop: asyncio.Event
    signal_feed_state: SignalFeedRuntimeState | None = None
    observe_state: ZGapObserveRuntimeState = field(default_factory=ZGapObserveRuntimeState)
    startup: ZGapStartupTimings = field(default_factory=ZGapStartupTimings)
    live_tasks: list[asyncio.Task[Any]] = field(default_factory=list)
    oms: Any = None
    apply_local_fill: bool = True
    sigma_warm: bool = False
    feeds_ready: bool = False
    sigma_estimator: EwmaVolatilityEstimator | None = None
    sigma_warmup: SigmaWarmupState = field(default_factory=SigmaWarmupState)
    facts_path: Path | None = None


def _git_sha() -> str:
    try:
        import subprocess

        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


class _TrackingShadowOMS:
    """Shadow OMS with submission counter for experimental observe proof."""

    def __init__(self) -> None:
        from tyrex_pm.execution.adapters import ShadowOMS

        self._inner = ShadowOMS()
        self.submit_count = 0

    async def submit(self, ap: Any, *, market_info: Any | None = None) -> str:
        self.submit_count += 1
        return await self._inner.submit(ap, market_info=market_info)

    async def cancel(self, ac: Any) -> str:
        return await self._inner.cancel(ac)


def _build_coordinator(app: AppConfig, *, live: bool) -> tuple[RuntimeCoordinator, Any, bool]:
    from tyrex_pm.execution.adapters import ShadowOMS

    if live:
        from tyrex_pm.execution.live_oms import LiveOMS
        from tyrex_pm.execution.oms import SingleWriterOMS
        from tyrex_pm.venue.polymarket.clob_env import try_create_clob_client
        from tyrex_pm.venue.polymarket.market_info import MarketInfoCache
        from tyrex_pm.venue.polymarket.clob_bridge import PyClobBridge

        live_clob = try_create_clob_client()
        if live_clob is None:
            raise RuntimeError("live runtime requires TYREX_PRIVATE_KEY and tyrex-pm[live]")
        clob_host = os.environ.get("TYREX_CLOB_HOST", "https://clob.polymarket.com")
        bridge = PyClobBridge(live_clob)
        writer = SingleWriterOMS(LiveOMS(bridge))
        writer.start()
        coord = RuntimeCoordinator(
            wallet=WalletStore(),
            orders=OrderStore(),
            health=HealthRuntime(),
            market_info_cache=MarketInfoCache(live_clob, host=clob_host),
        )
        return coord, writer, False
    wallet = WalletStore()
    if app.runtime.shadow_bootstrap is not None:
        apply_shadow_bootstrap(wallet, app.runtime.shadow_bootstrap)
    coord = RuntimeCoordinator(wallet=wallet, orders=OrderStore(), health=HealthRuntime())
    return coord, ShadowOMS(), True


async def bootstrap_session_runtime(
    *,
    app: AppConfig,
    run_name: str,
    repo_root: Path,
    runs_dir: Path | None = None,
    use_shadow_oms: bool | None = None,
) -> SessionRuntimeHandle:
    """Create coordinator, sink, and TimeAuthority for a session run."""
    assert app.z_gap is not None
    run_id = RunId(str(uuid.uuid4()))
    base = runs_dir or (repo_root / app.runtime.reporting.runs_dir / run_name)
    base.mkdir(parents=True, exist_ok=True)
    facts_path = base / "facts.jsonl"
    (base / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": str(run_id),
                "schema_version": 2,
                "git_sha": _git_sha(),
                "execution_mode": app.runtime.execution_mode.value,
                "run_kind": "z_gap_session",
                "run_name": run_name,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    sink = JsonlSink(facts_path)
    sink.__enter__()
    scenario_live = app.runtime.execution_mode == ExecutionMode.LIVE
    if use_shadow_oms is None:
        live = scenario_live
    else:
        live = not use_shadow_oms
    coord, oms, apply_local = _build_coordinator(app, live=live)
    if use_shadow_oms:
        oms = _TrackingShadowOMS()
        apply_local = True
    if app.runtime.market_data.enabled:
        ensure_market_state_store(coord, app)
    coord.time_authority = sample_offset(require_feeds_not_started=True)
    sigma_estimator = EwmaVolatilityEstimator(config=sigma_config_from_zg(app.z_gap))
    return SessionRuntimeHandle(
        repo_root=repo_root,
        runs_dir=base,
        run_id=run_id,
        coord=coord,
        sink=sink,
        stop=asyncio.Event(),
        oms=oms,
        apply_local_fill=apply_local,
        facts_path=facts_path,
        sigma_estimator=sigma_estimator,
    )


async def start_production_feeds(
    handle: SessionRuntimeHandle,
    app: AppConfig,
) -> None:
    """Start Binance + RTDS signal feeds into SignalStateStore."""
    assert app.z_gap is not None
    zg = app.z_gap

    def _on_health(payload: dict) -> None:
        handle_feed_health_callback(handle.sink, handle.run_id, handle.observe_state, dict(payload))

    t0 = time.monotonic()
    if signal_feeds_requested(app):
        handle.signal_feed_state = await start_signal_feeds(
            coord=handle.coord,
            app=app,
            stop=handle.stop,
            market_id=zg.market_id,
            event_start_ts=zg.event_start_ts,
            event_end_ts=zg.event_end_ts,
            on_health=_on_health,
        )
        if handle.coord.signal_state is None:
            raise RuntimeError("signal feeds failed: SignalStateStore not initialized")
        elapsed = round((time.monotonic() - t0) * 1000.0, 1)
        handle.startup.binance_connect_ms = elapsed
        handle.startup.rtds_connect_ms = elapsed
        handle.startup.signal_state_ready_ms = elapsed
        if handle.signal_feed_state.price_to_beat_tracker is not None:
            handle.startup.ptb_tracker_registered_ms = elapsed
        handle.feeds_ready = True
    handle.startup.total_startup_ms = round((time.monotonic() - t0) * 1000.0, 1)


def emit_runtime_started_fact(
    handle: SessionRuntimeHandle,
    app: AppConfig,
    *,
    runtime_mode: str,
    ptb_locked: bool,
) -> None:
    assert app.z_gap is not None
    handle.sink.write(
        make_fact(
            FACT_TYPE_Z_GAP_RUNTIME_STARTED,
            str(handle.run_id),
            {
                "runtime_mode": runtime_mode,
                "market_id": app.z_gap.market_id,
                "event_start_ts": app.z_gap.event_start_ts,
                "event_end_ts": app.z_gap.event_end_ts,
                "ptb_locked": ptb_locked,
                "sigma_warm": handle.sigma_warm,
                "feeds_ready": handle.feeds_ready,
                "entry_mode": app.z_gap.entry_mode,
            },
        )
    )


async def run_post_boundary_runtime(
    handle: SessionRuntimeHandle,
    app: AppConfig,
    *,
    execute: bool,
    ptb_locked: bool,
) -> int:
    """Invoke real observe or enforce loop after boundary PTB lock."""
    assert app.z_gap is not None
    runtime_mode = "enforce" if execute and app.z_gap.entry_mode == Z_GAP_ENTRY_MODE_ENFORCE else "observe"
    emit_runtime_started_fact(handle, app, runtime_mode=runtime_mode, ptb_locked=ptb_locked)

    if runtime_mode == "enforce":
        strategy = ZGapStrategy(app.z_gap)
        exit_code = await run_z_gap_enforce_loop(
            app=app,
            run_id=handle.run_id,
            coord=handle.coord,
            sink=handle.sink,
            strategy=strategy,
            oms=handle.oms,
            stop=handle.stop,
            apply_local_shadow_fill=handle.apply_local_fill,
            time_authority=handle.coord.time_authority,
            startup_timings=handle.startup,
            estimator=handle.sigma_estimator,
        )
    else:
        exit_code = await run_z_gap_observe_loop(
            app=app,
            run_id=handle.run_id,
            coord=handle.coord,
            sink=handle.sink,
            stop=handle.stop,
            signal_feed_state=handle.signal_feed_state,
            runtime_state=handle.observe_state,
            time_authority=handle.coord.time_authority,
            startup_timings=handle.startup,
            skip_late_start_check=True,
            estimator=handle.sigma_estimator,
        )
    return exit_code


async def shutdown_session_runtime(handle: SessionRuntimeHandle) -> None:
    if handle.signal_feed_state is not None:
        await stop_signal_feeds(handle.signal_feed_state)
        handle.signal_feed_state = None
    for task in list(handle.live_tasks):
        task.cancel()
    if handle.live_tasks:
        await asyncio.gather(*handle.live_tasks, return_exceptions=True)
    handle.live_tasks.clear()
    if handle.oms is not None and hasattr(handle.oms, "stop"):
        handle.oms.stop()
    handle.sink.__exit__(None, None, None)


async def run_z_gap_session_runtime(
    *,
    app: AppConfig,
    run_name: str,
    repo_root: Path,
    execute: bool,
    ptb_locked: bool,
    feeds_already_started: bool = False,
    sigma_already_warm: bool = False,
    handle: SessionRuntimeHandle | None = None,
) -> tuple[int, SessionRuntimeHandle]:
    """Full post-boundary runtime: reuse handle when feeds already started."""
    owns_handle = handle is None
    h = handle or await bootstrap_session_runtime(app=app, run_name=run_name, repo_root=repo_root)
    try:
        if not feeds_already_started:
            await start_production_feeds(h, app)
        if not sigma_already_warm:
            await warm_sigma_before_boundary(h, app)
        return await run_post_boundary_runtime(h, app, execute=execute, ptb_locked=ptb_locked), h
    except Exception:
        if owns_handle:
            await shutdown_session_runtime(h)
        raise
