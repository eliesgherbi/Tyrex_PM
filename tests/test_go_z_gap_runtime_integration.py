"""Integration tests: orchestrator invokes real Z-Gap runtime (not stubbed)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.ingestion.btc_5m_window_scheduler import Btc5mWindowPlan
from tyrex_pm.ingestion.price_to_beat_tracker import PTB_STATUS_OBSERVED
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE, Z_GAP_ENTRY_MODE_OBSERVE_ONLY
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.runtime.z_gap_boundary_gate import evaluate_boundary_ptb_gate
from tyrex_pm.runtime.z_gap_artifact_writer import write_calibration_ack_artifact
from tyrex_pm.runtime.z_gap_config_hash import compute_z_gap_config_fingerprint
from tyrex_pm.runtime.z_gap_session_orchestrator import (
    STATUS_TERMINAL,
    ZGapSessionOrchestrator,
)
from tyrex_pm.runtime.z_gap_session_runtime import (
    bootstrap_session_runtime,
    run_post_boundary_runtime,
    shutdown_session_runtime,
    start_production_feeds,
    warm_sigma_before_boundary,
)
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED, SignalStateStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.scenario_oms import OmsFillSpec, ScenarioOMS
from tyrex_pm.strategies.z_gap.shadow_harness import FD_RAW, write_preflight_artifacts

EVENT_START = 2_000_000.0
EVENT_END = EVENT_START + 300.0
K_PRICE = "100000.00"
TS = datetime.fromtimestamp(EVENT_START, tz=timezone.utc)


def _write_tick(path: Path, *, source_ts: datetime, price: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "source_ts": source_ts.isoformat(),
                    "recv_ts": source_ts.isoformat(),
                    "price": price,
                }
            )
            + "\n"
        )


def _meta() -> Btc5mMarketMetadata:
    return Btc5mMarketMetadata(
        market_id="btc_5m_20260703_1200",
        condition_id="0xabc",
        yes_token_id="111",
        no_token_id="222",
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        event_slug="btc-updown-5m-test",
        event_url="https://polymarket.com/event/btc-updown-5m-test",
    )


def _synced_time_authority(epoch: float) -> TimeAuthority:
    return TimeAuthority(
        sync_status="synced",
        offset_ms=0.0,
        uncertainty_ms=20.0,
        median_offset_ms=0.0,
        samples_requested=3,
        samples_kept=3,
        max_rtt_ms=50.0,
        _epoch_at_sync=epoch,
        _mono_at_sync=0.0,
    )


def _populate_signal_store(store: SignalStateStore, *, binance: str = "100150") -> None:
    store.update_binance(Decimal(binance), source_ts=TS, recv_ts=TS, stream="bookTicker")
    store.update_chainlink(Decimal(K_PRICE), source_ts=TS, recv_ts=TS)
    store.update_price_to_beat(
        Decimal(K_PRICE),
        status=FRESHNESS_OBSERVED,
        observed_ts=TS,
        lag_ms=100.0,
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


def _read_fact_types(facts_path: Path) -> set[str]:
    types: set[str] = set()
    for line in facts_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        types.add(row.get("fact_type", ""))
    return types


@pytest.fixture
def virtual_clock() -> list[float]:
    return [EVENT_START - 45.0]


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def orchestrator_setup(tmp_path: Path, virtual_clock: list[float], monkeypatch: pytest.MonkeyPatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    meta = _meta()
    tick_path = tmp_path / "chainlink_ticks.jsonl"
    _write_tick(tick_path, source_ts=TS, price=K_PRICE)

    write_preflight_artifacts(artifacts, market_id=meta.market_id)
    ptb_path = artifacts / "ptb_attestation.json"
    ptb = json.loads(ptb_path.read_text(encoding="utf-8"))
    ptb["K_derived"] = K_PRICE
    ptb["K_reference"] = K_PRICE
    ptb_path.write_text(json.dumps(ptb), encoding="utf-8")
    orch = ZGapSessionOrchestrator(
        repo_root=REPO_ROOT,
        scenario_file="config/scenarios/live_z_gap_tiny.yaml",
        strategy_file="config/strategies/z_gap.yaml",
        artifacts_dir=artifacts,
    )

    async def _advance_sleep(seconds: float) -> None:
        virtual_clock[0] += seconds

    orch.deps.now_ts = lambda: virtual_clock[0]
    orch.deps.sleep = _advance_sleep
    orch.deps.resolve_metadata = lambda _url: meta
    orch.deps.sidecar = None

    async def _fee_model(_app, _meta):
        return parse_fee_model_from_raw(FD_RAW)

    orch.deps.resolve_fee_model = _fee_model
    orch.deps.run_binance_check = AsyncMock(return_value={"ok": True})
    orch.deps.run_clock_check = lambda: (
        {"sync_status": "synced", "enforce_gate_pass": True, "time_authority_uncertainty_ms": 20.0},
        None,
    )

    def _boundary(**kwargs):
        kwargs.update(
            chainlink_log_path=tick_path,
            live_price=K_PRICE,
            live_status=PTB_STATUS_OBSERVED,
            live_lag_ms=100.0,
        )
        return evaluate_boundary_ptb_gate(**kwargs)

    orch.deps.boundary_evaluator = _boundary

    plan = Btc5mWindowPlan(
        window_start_ts=int(EVENT_START),
        window_end_ts=int(EVENT_END),
        event_slug=meta.event_slug,
        event_url=meta.event_url or "",
        wake_at_ts=virtual_clock[0],
    )
    monkeypatch.setattr(
        "tyrex_pm.runtime.z_gap_session_orchestrator.select_session_window",
        lambda **_: (plan, ()),
    )

    app_for_fp = orch._load_app(entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY)
    fingerprint = compute_z_gap_config_fingerprint(
        app_for_fp,
        strategy_file=REPO_ROOT / orch.strategy_file,
        scenario_file=REPO_ROOT / orch.scenario_file,
    )
    write_calibration_ack_artifact(
        artifacts / "calibration_lite_review.json",
        fingerprint=fingerprint,
        now_ts=virtual_clock[0],
        expiry_hours=168.0,
    )

    async def _patched_bootstrap(**kwargs):
        app = kwargs["app"]
        app = replace(
            app,
            runtime=replace(app.runtime, execution_mode=ExecutionMode.SHADOW),
        )
        handle = await bootstrap_session_runtime(
            app=app,
            run_name=kwargs["run_name"],
            repo_root=kwargs["repo_root"],
            runs_dir=kwargs.get("runs_dir"),
        )
        handle.coord.signal_state = SignalStateStore()
        _populate_signal_store(handle.coord.signal_state)
        handle.coord.market_state = MarketStateStore(default_max_age_s=30.0)
        _populate_books(handle.coord.market_state)
        handle.coord.time_authority = _synced_time_authority(virtual_clock[0])
        return handle

    async def _patched_feeds(handle, _app):
        handle.feeds_ready = True

    async def _patched_warm(handle, _app, **kwargs):
        handle.sigma_warm = True
        return True

    real_observe = None

    def _capture_observe():
        from tyrex_pm.runtime import z_gap_session_runtime as rt

        nonlocal real_observe
        real_observe = rt.run_z_gap_observe_loop

        async def _short_observe(**kw):
            kw["max_ticks"] = 2
            kw["tick_interval_s"] = 0.001
            kw["fd_raw"] = FD_RAW
            kw["time_authority"] = _synced_time_authority(virtual_clock[0])
            return await real_observe(**kw)

        monkeypatch.setattr(rt, "run_z_gap_observe_loop", _short_observe)
        return rt.run_post_boundary_runtime

    monkeypatch.setattr(
        "tyrex_pm.runtime.z_gap_session_orchestrator.bootstrap_session_runtime",
        _patched_bootstrap,
    )
    monkeypatch.setattr(
        "tyrex_pm.runtime.z_gap_session_orchestrator.start_production_feeds",
        _patched_feeds,
    )
    monkeypatch.setattr(
        "tyrex_pm.runtime.z_gap_session_orchestrator.warm_sigma_before_boundary",
        _patched_warm,
    )

    return orch, virtual_clock, _capture_observe, real_observe


@pytest.mark.asyncio
async def test_orchestrator_observe_runtime_emits_required_facts(orchestrator_setup) -> None:
    orch, virtual_clock, capture_observe, _ = orchestrator_setup
    runtime_module = capture_observe()

    with patch(
        "tyrex_pm.runtime.z_gap_session_orchestrator.run_post_boundary_runtime",
        wraps=runtime_module,
    ) as runtime_spy:
        result = await orch.run_session(
            run_name="observe_integration",
            next_window=True,
            execute=False,
            interactive=False,
            observe_only=True,
        )

    assert runtime_spy.call_count == 1, f"status={result.status} blockers={result.blockers}"
    assert result.status == STATUS_TERMINAL
    assert result.facts_path is not None
    assert result.oms_submissions == 0

    fact_types = _read_fact_types(Path(result.facts_path))
    assert "z_gap_runtime_started" in fact_types
    assert "model_state_snapshot" in fact_types
    assert "edge_evaluated" in fact_types
    assert fact_types & {"z_gap_entry_eval", "z_gap_entry_skip"}
    assert "calibration_sample" in fact_types
    assert "z_gap_terminal_summary" in fact_types


@pytest.mark.asyncio
async def test_orchestrator_fails_when_runtime_stubbed(orchestrator_setup) -> None:
    orch, _, _, _ = orchestrator_setup

    async def _stub_runtime(*_args, **_kwargs):
        return 0

    with patch(
        "tyrex_pm.runtime.z_gap_session_orchestrator.run_post_boundary_runtime",
        new=_stub_runtime,
    ):
        result = await orch.run_session(
            run_name="stubbed_runtime",
            next_window=True,
            execute=False,
            interactive=False,
            observe_only=True,
        )

    assert result.facts_path is not None
    fact_types = _read_fact_types(Path(result.facts_path))
    assert "z_gap_runtime_started" not in fact_types


@pytest.mark.asyncio
async def test_shadow_enforce_runtime_emits_pipeline_facts(tmp_path: Path) -> None:
    """Production enforce loop (same path as run_post_boundary_runtime enforce mode)."""
    from tyrex_pm.strategies.z_gap.shadow_harness import ShadowHarness, TickSpec

    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=ScenarioOMS(
            buy_fills=[OmsFillSpec(status="matched", taking_amount="8")],
            sell_fills=[OmsFillSpec(status="matched", making_amount="8")],
        ),
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    await harness.run_ticks(
        [
            TickSpec(now_ts=EVENT_START + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 91, binance_price=Decimal("100150")),
            TickSpec(now_ts=EVENT_START + 92, binance_price=Decimal("99800")),
            TickSpec(now_ts=EVENT_START + 93, binance_price=Decimal("99800")),
        ]
    )
    fact_types = {f["fact_type"] for f in harness.sink.facts}
    assert fact_types & {"z_gap_entry_plan", "intent_created"}
    assert "z_gap_position_activated" in fact_types or "z_gap_entry_fill" in fact_types
    assert fact_types & {
        "z_gap_exit_submitted",
        "model_exit_triggered",
        "z_gap_terminal_summary",
    }


@pytest.mark.asyncio
async def test_run_post_boundary_invokes_real_enforce_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tyrex_pm.strategies.z_gap.shadow_harness import ShadowHarness

    harness = ShadowHarness.create(
        tmp_path=tmp_path,
        event_start_ts=EVENT_START,
        event_end_ts=EVENT_END,
        oms=ScenarioOMS(buy_fills=[OmsFillSpec(status="expired", taking_amount="")]),
    )
    handle = await bootstrap_session_runtime(
        app=harness.app,
        run_name="enforce_delegate",
        repo_root=REPO_ROOT,
        runs_dir=tmp_path / "runs" / "enforce_delegate",
    )
    handle.coord = harness.coord
    handle.oms = harness.oms
    handle.feeds_ready = True
    handle.sigma_warm = True

    from tyrex_pm.runtime import z_gap_session_runtime as rt

    real_enforce = rt.run_z_gap_enforce_loop
    calls: list[int] = []

    async def _track_enforce(**kw):
        calls.append(1)
        kw["max_ticks"] = 1
        kw["fd_raw"] = FD_RAW
        return await real_enforce(**kw)

    monkeypatch.setattr(rt, "run_z_gap_enforce_loop", _track_enforce)
    exit_code = await run_post_boundary_runtime(
        handle,
        harness.app,
        execute=True,
        ptb_locked=True,
    )
    assert calls == [1]
    assert handle.facts_path is not None
    assert "z_gap_runtime_started" in _read_fact_types(handle.facts_path)
    await shutdown_session_runtime(handle)
