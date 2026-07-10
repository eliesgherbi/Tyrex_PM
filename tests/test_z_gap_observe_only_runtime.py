"""Tests for Z-Gap observe-only runtime (A0.5)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import time
from unittest.mock import MagicMock

import pytest

from tyrex_pm.runtime.time_authority import TimeAuthority

from tyrex_pm.core.ids import RunId
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator, SigmaConfig
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.z_gap_run import ObserveTickContext, run_observe_tick, run_z_gap_observe_loop
from tyrex_pm.core.ids import TokenId
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED, SignalStateStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.facts import ZGapObserveRuntimeState

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
FD_RAW = {"c": "0xabc", "fd": {"r": 0.07, "e": 1, "to": True}}
EVENT_END = 1780000300.0


def _synced_time_authority() -> TimeAuthority:
    return TimeAuthority(
        sync_status="synced",
        offset_ms=0.0,
        uncertainty_ms=50.0,
        median_offset_ms=0.0,
        samples_requested=5,
        samples_kept=5,
        max_rtt_ms=100.0,
        _epoch_at_sync=time.time(),
        _mono_at_sync=time.monotonic(),
    )


def _app():
    app = parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "25", "portfolio_cap_usd": "100"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False, "max_wallet_age_s": 120},
            "concurrency": {"max_orders_in_flight": 4},
            "readiness": {
                "require_wallet_sync": False,
                "max_wallet_age_s_live": 120,
                "require_heartbeat_live": False,
                "require_user_ws_live": False,
            },
        },
        strategy={
            "kind": "z_gap",
            "enabled": True,
            "z_gap": {
                "market_id": "btc_5m_20260703_1200",
                "condition_id": "0xabc",
                "yes_token_id": "111",
                "no_token_id": "222",
                "event_start_ts": 1780000000,
                "event_end_ts": 9999999999,
                "sigma": {"min_samples_s": 1, "sample_interval_s": 0},
                "entry": {
                    "theta_take": "0.05",
                    "z_band": ["0.8", "2.2"],
                    "tau_band_s": [60, 210],
                    "basis_max_bps": "3",
                    "expected_slippage_ticks": 1,
                },
            },
        },
        runtime={
            "execution_mode": "live",
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "signal_feeds": {"enabled": True},
        },
    )
    assert app.z_gap is not None
    app = replace(app, z_gap=replace(app.z_gap, event_start_ts=time.time() + 3600.0))
    return app


def _coord() -> RuntimeCoordinator:
    return RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
    )


def _populate_signal_store(store: SignalStateStore) -> None:
    store.update_binance(Decimal("100100"), source_ts=TS, recv_ts=TS, stream="bookTicker")
    store.update_chainlink(Decimal("100000"), source_ts=TS, recv_ts=TS)
    store.update_price_to_beat(
        Decimal("100000"),
        status=FRESHNESS_OBSERVED,
        observed_ts=TS,
        lag_ms=500.0,
    )
    store.mark_binance_connected(connected=True)
    store.mark_chainlink_connected(connected=True)


def _populate_books(store: MarketStateStore) -> None:
    store.apply_snapshot(
        make_snapshot(
            TokenId("111"),
            bids=[(Decimal("0.60"), Decimal("1000"))],
            asks=[(Decimal("0.61"), Decimal("1000"))],
            ts=TS,
        )
    )
    store.apply_snapshot(
        make_snapshot(
            TokenId("222"),
            bids=[(Decimal("0.38"), Decimal("1000"))],
            asks=[(Decimal("0.39"), Decimal("1000"))],
            ts=TS,
        )
    )


class _ListSink:
    def __init__(self) -> None:
        self.facts: list[dict] = []

    def write(self, obj: dict) -> None:
        self.facts.append(obj)


def _warm_estimator(est: EwmaVolatilityEstimator) -> None:
    for i in range(5):
        est.update(Decimal("100000") + Decimal(i), TS)


@pytest.mark.asyncio
async def test_observe_loop_emits_facts_and_terminal_summary(tmp_path) -> None:
    app = _app()
    coord = _coord()
    coord.signal_state = SignalStateStore()
    _populate_signal_store(coord.signal_state)
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    _populate_books(coord.market_state)

    sink = _ListSink()
    run_id = RunId("zg_test_1")

    exit_code = await run_z_gap_observe_loop(
        app=app,
        run_id=run_id,
        coord=coord,
        sink=sink,
        max_ticks=1,
        tick_interval_s=0.01,
        fd_raw=FD_RAW,
        calibration_samples_path=tmp_path / "calibration_samples.jsonl",
        time_authority=_synced_time_authority(),
    )

    fact_types = {f["fact_type"] for f in sink.facts}
    assert "model_state_snapshot" in fact_types
    assert "edge_evaluated" in fact_types
    assert "z_gap_terminal_summary" in fact_types
    assert "calibration_sample" in fact_types
    assert exit_code == 0


@pytest.mark.asyncio
async def test_no_oms_submit_or_intent_or_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app()
    coord = _coord()
    coord.signal_state = SignalStateStore()
    _populate_signal_store(coord.signal_state)
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    _populate_books(coord.market_state)

    oms_submit_count = 0
    intent_count = 0
    allocation_mutations = 0

    def _oms_submit(*args, **kwargs):
        nonlocal oms_submit_count
        oms_submit_count += 1

    def _intent(*args, **kwargs):
        nonlocal intent_count
        intent_count += 1

    monkeypatch.setattr("tyrex_pm.runtime.pipeline.process_intent_work_unit", _intent)

    ledger = MagicMock()

    def _ledger_mutate(*args, **kwargs):
        nonlocal allocation_mutations
        allocation_mutations += 1

    ledger.reserve = _ledger_mutate
    ledger.commit = _ledger_mutate
    coord.allocation_ledger = ledger

    sink = _ListSink()
    await run_z_gap_observe_loop(
        app=app,
        run_id=RunId("zg_no_oms"),
        coord=coord,
        sink=sink,
        max_ticks=2,
        tick_interval_s=0.01,
        fd_raw=FD_RAW,
        calibration_samples_path=None,
        time_authority=_synced_time_authority(),
    )

    assert oms_submit_count == 0
    assert intent_count == 0
    assert allocation_mutations == 0


def test_observe_tick_handles_missing_books_without_crash() -> None:
    app = _app()
    coord = _coord()
    coord.signal_state = SignalStateStore()
    _populate_signal_store(coord.signal_state)
    sink = _ListSink()
    est = EwmaVolatilityEstimator(config=SigmaConfig(min_samples_s=1, sample_interval_s=0))
    _warm_estimator(est)

    ctx = ObserveTickContext(
        app=app,
        zg=app.z_gap,
        run_id=RunId("tick_missing_books"),
        coord=coord,
        sink=sink,
        state=ZGapObserveRuntimeState(),
        estimator=est,
        fee_model=parse_fee_model_from_raw(FD_RAW),
        now_ts=EVENT_END - 120,
    )
    evaln = run_observe_tick(ctx)
    assert evaln is not None
    assert evaln.reason_code is not None


def test_observe_tick_handles_not_ready_model_without_crash() -> None:
    app = _app()
    coord = _coord()
    coord.signal_state = SignalStateStore()
    store = coord.signal_state
    store.update_binance(Decimal("100100"), source_ts=TS, recv_ts=TS)
    store.update_chainlink(Decimal("100000"), source_ts=TS, recv_ts=TS)
    sink = _ListSink()
    est = EwmaVolatilityEstimator(config=SigmaConfig(min_samples_s=1000))

    ctx = ObserveTickContext(
        app=app,
        zg=app.z_gap,
        run_id=RunId("tick_sigma_warmup"),
        coord=coord,
        sink=sink,
        state=ZGapObserveRuntimeState(),
        estimator=est,
        fee_model=parse_fee_model_from_raw(FD_RAW),
        now_ts=EVENT_END - 120,
    )
    evaln = run_observe_tick(ctx)
    assert evaln is not None
