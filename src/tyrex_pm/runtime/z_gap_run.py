"""Z-Gap observe-only runtime loop (A0.5).

Evaluates sigma, fair value, dynamic fee edge, and entry gates live.
Never submits orders, never builds IntentWorkUnit, never mutates allocation ledger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.core.ids import RunId
from tyrex_pm.market_data.book_read import read_pair_books
from tyrex_pm.quant.binary_fair_value import FairValueInput, compute_fair_value
from tyrex_pm.quant.edge import compute_edge
from tyrex_pm.quant.fees import FeeModel, parse_fee_model_from_market_info, parse_fee_model_from_raw
from tyrex_pm.quant.model_sanity import ModelSanityConfig, evaluate_model_numeric_sanity
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator, SigmaConfig, VolatilitySnapshot
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_CALIBRATION_SAMPLE,
    FACT_TYPE_CLOCK_SYNC,
    FACT_TYPE_Z_GAP_STARTUP_TIMING,
    FACT_TYPE_Z_GAP_WINDOW_SKIPPED_LATE_START,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    AppConfig,
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    ZGapEntryConfig,
    ZGapStrategyConfig,
    _parse_z_gap_entry,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.signal_feed_runtime import SignalFeedRuntimeState
from tyrex_pm.runtime.time_authority import (
    DEFAULT_RESYNC_INTERVAL_S,
    DEFAULT_SAMPLES_TOTAL,
    SYNC_STATUS_FAILED,
    SYNC_STATUS_SYNCED,
    TimeAuthority,
    sync_time_authority,
)
from tyrex_pm.runtime.z_gap_live import z_gap_preflight_dir
from tyrex_pm.state.signal_state_store import FRESHNESS_FRESH, SignalStateStore
from tyrex_pm.strategies.z_gap.calibration_samples import (
    append_calibration_sample,
    build_calibration_sample_fact_payload,
    build_calibration_sample_row,
    is_calibration_usable_ptb,
)
from tyrex_pm.strategies.z_gap import facts as zg_facts
from tyrex_pm.strategies.z_gap.entry_eval import evaluate_z_gap_entry
from tyrex_pm.strategies.z_gap.facts import ZGapObserveRuntimeState

log = logging.getLogger(__name__)

DEFAULT_TICK_INTERVAL_S = 1.0
DEFAULT_TICK_SIZE = Decimal("0.01")
DEFAULT_MIN_PRESTART_SECONDS = 20.0


@dataclass
class ZGapStartupTimings:
    metadata_resolve_ms: float | None = None
    rtds_connect_ms: float | None = None
    binance_connect_ms: float | None = None
    clob_bootstrap_ms: float | None = None
    signal_state_ready_ms: float | None = None
    ptb_tracker_registered_ms: float | None = None
    sigma_warmup_ms: float | None = None
    total_startup_ms: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata_resolve_ms": self.metadata_resolve_ms,
            "rtds_connect_ms": self.rtds_connect_ms,
            "binance_connect_ms": self.binance_connect_ms,
            "clob_bootstrap_ms": self.clob_bootstrap_ms,
            "signal_state_ready_ms": self.signal_state_ready_ms,
            "ptb_tracker_registered_ms": self.ptb_tracker_registered_ms,
            "sigma_warmup_ms": self.sigma_warmup_ms,
            "total_startup_ms": self.total_startup_ms,
        }


@dataclass
class ObserveTickContext:
    """Injectable dependencies for a single observe tick (testing)."""

    app: AppConfig
    zg: ZGapStrategyConfig
    run_id: RunId
    coord: RuntimeCoordinator
    sink: JsonlSink | object
    state: ZGapObserveRuntimeState
    estimator: EwmaVolatilityEstimator
    fee_model: FeeModel | None = None
    time_authority: TimeAuthority | None = None
    tick_size: Decimal = DEFAULT_TICK_SIZE
    entry_cfg: ZGapEntryConfig | None = None
    now_ts: float | None = None
    lifecycle: object | None = None


def corrected_now_ts(time_authority: TimeAuthority | None, now_ts: float | None = None) -> float:
    if now_ts is not None:
        return now_ts
    if time_authority is not None:
        return time_authority.corrected_epoch()
    return time.time()


def corrected_now_dt(time_authority: TimeAuthority | None, now_ts: float | None = None) -> datetime:
    ts = corrected_now_ts(time_authority, now_ts)
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def is_late_window_start(
    *,
    event_start_ts: float | None,
    time_authority: TimeAuthority | None,
    min_prestart_seconds: float = DEFAULT_MIN_PRESTART_SECONDS,
    now_ts: float | None = None,
) -> bool:
    if event_start_ts is None:
        return False
    now = corrected_now_ts(time_authority, now_ts)
    return now > float(event_start_ts) - min_prestart_seconds


def emit_clock_sync_fact(
    sink: JsonlSink | object,
    run_id: RunId,
    time_authority: TimeAuthority,
) -> None:
    sink.write(
        make_fact(
            FACT_TYPE_CLOCK_SYNC,
            str(run_id),
            time_authority.clock_sync_payload(),
        )
    )


def emit_startup_timing_fact(
    sink: JsonlSink | object,
    run_id: RunId,
    timings: ZGapStartupTimings,
) -> None:
    sink.write(make_fact(FACT_TYPE_Z_GAP_STARTUP_TIMING, str(run_id), timings.to_dict()))


def emit_late_start_skip_fact(
    sink: JsonlSink | object,
    run_id: RunId,
    *,
    market_id: str,
    event_start_ts: float,
    min_prestart_seconds: float,
    now_ts: float,
) -> None:
    sink.write(
        make_fact(
            FACT_TYPE_Z_GAP_WINDOW_SKIPPED_LATE_START,
            str(run_id),
            {
                "market_id": market_id,
                "event_start_ts": event_start_ts,
                "now_ts": now_ts,
                "min_prestart_seconds": min_prestart_seconds,
                "late_by_s": now_ts - (event_start_ts - min_prestart_seconds),
            },
        )
    )


def _entry_cfg_for(app: AppConfig) -> ZGapEntryConfig:
    if app.z_gap is not None and app.z_gap.entry is not None:
        return app.z_gap.entry
    return _parse_z_gap_entry({})


def load_clock_drift_ms(artifacts_dir: Path | None = None) -> float | None:
    base = artifacts_dir or z_gap_preflight_dir()
    path = base / "clock_sanity.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return float(data.get("clock_drift_ms"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def sigma_config_from_zg(zg: ZGapStrategyConfig) -> SigmaConfig:
    sg = zg.sigma
    return SigmaConfig(
        estimator=sg.estimator,
        half_life_s=sg.half_life_s,
        min_samples_s=sg.min_samples_s,
        jump_guard=sg.jump_guard,
        jump_threshold_sigma=sg.jump_threshold_sigma,
        sample_interval_s=sg.sample_interval_s,
        tau_floor_s=sg.tau_floor_s,
    )


def model_sanity_config_from_zg(zg: ZGapStrategyConfig) -> ModelSanityConfig:
    ms = zg.model_sanity
    return ModelSanityConfig(
        warn_abs_z=ms.warn_abs_z,
        block_abs_z=ms.block_abs_z,
        sigma_ratio_warn_min=ms.sigma_ratio_warn_min,
        sigma_ratio_warn_max=ms.sigma_ratio_warn_max,
    )


def compute_tau_s(event_end_ts: float | None, now_ts: float) -> float | None:
    if event_end_ts is None:
        return None
    return max(0.0, float(event_end_ts) - now_ts)


async def resolve_fee_model(
    coord: RuntimeCoordinator,
    zg: ZGapStrategyConfig,
    *,
    fd_raw: dict[str, Any] | None = None,
) -> FeeModel:
    if fd_raw is not None:
        return parse_fee_model_from_raw(
            fd_raw,
            condition_id=zg.condition_id,
            market_id=zg.market_id,
        )
    cache = coord.market_info_cache
    if cache is not None:
        try:
            mi = await cache.get(zg.yes_token_id)
            return parse_fee_model_from_market_info(mi, market_id=zg.market_id)
        except Exception:
            log.debug("fee model resolve via MarketInfoCache failed", exc_info=True)
    return parse_fee_model_from_raw(
        None,
        condition_id=zg.condition_id,
        market_id=zg.market_id,
    )


def run_observe_tick(ctx: ObserveTickContext, *, allow_enforce: bool = False) -> ZGapEntryEvaluation | None:
    """Execute one evaluation tick. Observe-only unless ``allow_enforce``."""
    assert ctx.app.z_gap is not None
    if not allow_enforce and ctx.app.z_gap.entry_mode != Z_GAP_ENTRY_MODE_OBSERVE_ONLY:
        raise RuntimeError("observe tick requires entry_mode=observe_only")

    zg = ctx.zg
    entry_cfg = ctx.entry_cfg or _entry_cfg_for(ctx.app)
    now_ts = corrected_now_ts(ctx.time_authority, ctx.now_ts)
    now_dt = corrected_now_dt(ctx.time_authority, ctx.now_ts)

    signal_store: SignalStateStore | None = ctx.coord.signal_state
    if signal_store is None:
        log.warning("z_gap observe tick: signal_state missing")
        return None

    signal = signal_store.snapshot(now_dt)
    ctx.state.loop_ran = True
    ctx.state.total_ticks += 1
    ctx.state.last_signal = signal
    if signal.binance_freshness == FRESHNESS_FRESH and signal.chainlink_freshness == FRESHNESS_FRESH:
        ctx.state.feed_fresh_ticks += 1

    zg_facts.emit_signal_feed_health(ctx.sink, ctx.run_id, ctx.state, signal)
    zg_facts.emit_basis_computed(ctx.sink, ctx.run_id, ctx.state, signal)
    zg_facts.emit_price_to_beat_observed(
        ctx.sink,
        ctx.run_id,
        ctx.state,
        market_id=zg.market_id,
        signal=signal,
        event_start_ts=zg.event_start_ts,
        event_end_ts=zg.event_end_ts,
    )

    vol: VolatilitySnapshot = ctx.estimator.snapshot()
    vol_obs = signal_store.volatility_price_observation(now_dt)
    if vol_obs is not None:
        vol = ctx.estimator.update(vol_obs[0], vol_obs[1])
    elif signal.binance_price is not None and signal.binance_recv_ts is not None:
        vol = ctx.estimator.update(signal.binance_price, signal.binance_recv_ts)
    if vol.ready and ctx.state.sigma_ready_first_ts is None:
        ctx.state.sigma_ready_first_ts = now_dt

    tau_s = compute_tau_s(zg.event_end_ts, now_ts)
    fair = compute_fair_value(
        FairValueInput(
            S=signal.binance_price,
            K=signal.price_to_beat,
            sigma=vol.sigma,
            tau_s=tau_s,
            tau_floor_s=zg.sigma.tau_floor_s,
            snapshot_ts=now_dt,
        ),
        vol=vol,
    )
    zg_facts.emit_model_state_snapshot(ctx.sink, ctx.run_id, ctx.state, fair, vol)

    sanity_cfg = model_sanity_config_from_zg(zg)
    block_entries = ctx.app.z_gap.entry_mode == Z_GAP_ENTRY_MODE_ENFORCE
    sanity = evaluate_model_numeric_sanity(
        fair,
        cfg=sanity_cfg,
        block_entries=block_entries,
        tau_floor_s=zg.sigma.tau_floor_s,
    )
    if sanity.warn or sanity.block_entry:
        zg_facts.emit_model_numeric_anomaly(ctx.sink, ctx.run_id, ctx.state, fair, sanity)

    from tyrex_pm.state.market_store import MarketStateStore

    market_state: MarketStateStore | None = ctx.coord.market_state
    max_book_age_s = float(ctx.app.runtime.market_data.max_book_age_s)
    books = read_pair_books(
        market_state,
        yes_token_id=zg.yes_token_id,
        no_token_id=zg.no_token_id,
        max_book_age_s=max_book_age_s,
        now=now_dt,
        time_authority=ctx.time_authority,
    )

    fee_model = ctx.fee_model
    if fee_model is not None:
        ask_for_phi = books.up.ask or books.down.ask
        zg_facts.emit_fee_model_resolved(
            ctx.sink,
            ctx.run_id,
            ctx.state,
            fee_model,
            price=ask_for_phi,
            condition_id=zg.condition_id,
            market_id=zg.market_id,
        )

    slippage = Decimal(entry_cfg.expected_slippage_ticks) * ctx.tick_size
    edge = compute_edge(
        fair,
        ask_up=books.up.ask,
        ask_down=books.down.ask,
        fee_model=fee_model,
        expected_slippage_up=slippage,
        expected_slippage_down=slippage,
    )
    zg_facts.emit_edge_evaluated(ctx.sink, ctx.run_id, ctx.state, fair, edge)

    evaln = evaluate_z_gap_entry(
        signal=signal,
        fair=fair,
        edge=edge,
        vol=vol,
        books=books,
        entry_cfg=entry_cfg,
        fee_model=fee_model,
        time_authority=ctx.time_authority,
        entry_mode=ctx.app.z_gap.entry_mode,
        decision_ts=now_dt,
        lifecycle=ctx.lifecycle,
        numeric_anomaly_block=sanity.block_entry,
    )
    zg_facts.emit_entry_decision(ctx.sink, ctx.run_id, ctx.state, evaln)
    return evaln


def _operational_pass(state: ZGapObserveRuntimeState) -> bool:
    return bool(
        state.loop_ran
        and state.total_ticks > 0
        and state.facts_emitted
        and (state.last_fair is not None or state.evaluation_count > 0)
    )


async def run_z_gap_observe_loop(
    *,
    app: AppConfig,
    run_id: RunId,
    coord: RuntimeCoordinator,
    sink: JsonlSink | object,
    stop: asyncio.Event | None = None,
    signal_feed_state: SignalFeedRuntimeState | None = None,
    tick_interval_s: float = DEFAULT_TICK_INTERVAL_S,
    max_ticks: int | None = None,
    fd_raw: dict[str, Any] | None = None,
    calibration_samples_path: Path | None = None,
    runtime_state: ZGapObserveRuntimeState | None = None,
    time_authority: TimeAuthority | None = None,
    startup_timings: ZGapStartupTimings | None = None,
    min_prestart_seconds: float = DEFAULT_MIN_PRESTART_SECONDS,
    skip_late_start_check: bool = False,
    estimator: EwmaVolatilityEstimator | None = None,
) -> int:
    """Observe-only loop until stop, event end, or max_ticks."""
    assert app.z_gap is not None
    zg = app.z_gap
    if zg.entry_mode != Z_GAP_ENTRY_MODE_OBSERVE_ONLY:
        log.error("z_gap observe loop refused: entry_mode=%s (enforce blocked at preflight)", zg.entry_mode)
        return 1

    state = runtime_state or ZGapObserveRuntimeState()
    est = estimator or EwmaVolatilityEstimator(config=sigma_config_from_zg(zg))
    fee_model = await resolve_fee_model(coord, zg, fd_raw=fd_raw)

    ta = time_authority or coord.time_authority
    if ta is None:
        log.error("z_gap observe: TimeAuthority missing — startup sync must run before feeds")
        ta = TimeAuthority(sync_status=SYNC_STATUS_FAILED, samples_requested=DEFAULT_SAMPLES_TOTAL, samples_kept=0)
        coord.time_authority = ta
    elif ta.sync_status != SYNC_STATUS_SYNCED:
        log.warning(
            "z_gap observe: TimeAuthority not synced at startup sync_status=%s uncertainty_ms=%s",
            ta.sync_status,
            ta.uncertainty_ms,
        )
    emit_clock_sync_fact(sink, run_id, ta)
    if ta.observe_warning:
        log.warning(
            "z_gap clock sync observe_warning sync_status=%s uncertainty_ms=%s os_drift_ms=%s",
            ta.sync_status,
            ta.uncertainty_ms,
            ta.os_drift_ms,
        )

    if startup_timings is not None:
        emit_startup_timing_fact(sink, run_id, startup_timings)

    if not skip_late_start_check and is_late_window_start(
        event_start_ts=zg.event_start_ts,
        time_authority=ta,
        min_prestart_seconds=min_prestart_seconds,
    ):
        now_ts = ta.corrected_epoch()
        emit_late_start_skip_fact(
            sink,
            run_id,
            market_id=zg.market_id,
            event_start_ts=float(zg.event_start_ts or 0),
            min_prestart_seconds=min_prestart_seconds,
            now_ts=now_ts,
        )
        log.error(
            "z_gap observe loop skipped: late window start market_id=%s event_start_ts=%s",
            zg.market_id,
            zg.event_start_ts,
        )
        return 1

    tick_size = DEFAULT_TICK_SIZE
    cache = coord.market_info_cache
    if cache is not None:
        try:
            mi = await cache.get(zg.yes_token_id)
            if mi.tick_size is not None:
                tick_size = mi.tick_size
        except Exception:
            pass

    ticks = 0
    last_vol = None
    last_signal = None
    last_resync_mono = time.monotonic()

    log.info(
        "z_gap observe-only loop starting market_id=%s entry_mode=%s sync_status=%s uncertainty_ms=%s (no OMS)",
        zg.market_id,
        zg.entry_mode,
        ta.sync_status,
        ta.uncertainty_ms,
    )

    while True:
        if stop is not None and stop.is_set():
            break
        if max_ticks is not None and ticks >= max_ticks:
            break
        now_epoch = ta.corrected_epoch()
        if zg.event_end_ts is not None and max_ticks is None and now_epoch >= float(zg.event_end_ts):
            break

        if time.monotonic() - last_resync_mono >= DEFAULT_RESYNC_INTERVAL_S:
            refreshed = sync_time_authority(require_feeds_not_started=False)
            warning = ta.apply_resync(refreshed)
            last_resync_mono = time.monotonic()
            emit_clock_sync_fact(sink, run_id, ta)
            if warning:
                log.warning("z_gap %s", warning)
                sink.write(
                    make_fact(
                        "clock_step_detected",
                        str(run_id),
                        {"warning": warning, **ta.clock_sync_payload()},
                    )
                )

        ctx = ObserveTickContext(
            app=app,
            zg=zg,
            run_id=run_id,
            coord=coord,
            sink=sink,
            state=state,
            estimator=est,
            fee_model=fee_model,
            time_authority=ta,
            tick_size=tick_size,
        )
        try:
            run_observe_tick(ctx)
        except Exception:
            log.exception("z_gap observe tick failed")
        else:
            last_signal = state.last_signal
            last_vol = est.snapshot()
            ticks += 1

        if max_ticks is not None and ticks >= max_ticks:
            break
        if stop is not None and stop.is_set():
            break
        if zg.event_end_ts is not None and max_ticks is None and ta.corrected_epoch() >= float(zg.event_end_ts):
            break
        await asyncio.sleep(tick_interval_s)

    last_evaln = state.last_evaln
    if last_evaln is not None:
        row = build_calibration_sample_row(
            market_id=zg.market_id,
            condition_id=zg.condition_id,
            entry_mode=zg.entry_mode,
            evaln=last_evaln,
            fair=state.last_fair,
            edge=state.last_edge,
            vol=last_vol,
            signal=last_signal,
            fee_model=fee_model,
            resolved_outcome=None,
            calibration_usable=is_calibration_usable_ptb(last_signal),
        )
        append_calibration_sample(row, path=calibration_samples_path)
        state.calibration_sample_written = True
        sink.write(
            make_fact(
                FACT_TYPE_CALIBRATION_SAMPLE,
                str(run_id),
                build_calibration_sample_fact_payload(row),
            )
        )

    operational = _operational_pass(state)
    zg_facts.emit_terminal_summary(
        sink,
        run_id,
        state,
        entry_mode=zg.entry_mode,
        market_id=zg.market_id,
        condition_id=zg.condition_id,
        signal=last_signal,
        resolved_outcome=None,
        operational_pass=operational,
    )

    if signal_feed_state is not None:
        from tyrex_pm.runtime.signal_feed_runtime import stop_signal_feeds

        await stop_signal_feeds(signal_feed_state)

    log.info(
        "z_gap observe-only loop finished evaluations=%s would_enter=%s operational_pass=%s",
        state.evaluation_count,
        state.would_enter_count,
        operational,
    )
    return 0 if operational else 1


# A0.1 placeholder alias — replaced by observe loop.
async def run_z_gap_placeholder_loop(
    *,
    app: AppConfig,
    run_id: RunId,
    sink: JsonlSink | object,
    coord: RuntimeCoordinator | None = None,
    stop: asyncio.Event | None = None,
    signal_feed_state: SignalFeedRuntimeState | None = None,
) -> int:
    if coord is None:
        log.error("z_gap observe loop requires RuntimeCoordinator")
        return 1
    return await run_z_gap_observe_loop(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        stop=stop,
        signal_feed_state=signal_feed_state,
        max_ticks=1,
        tick_interval_s=0.01,
    )
