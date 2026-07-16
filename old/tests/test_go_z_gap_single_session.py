"""End-to-end single-session orchestrator tests with virtual time."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.ingestion.market_discovery import BTC_5M_WINDOW_S
from tyrex_pm.quant.fees import FeeModel
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.z_gap_artifact_writer import (
    write_calibration_ack_artifact,
    write_clock_artifact,
    write_connectivity_artifact,
    write_fee_curve_artifact,
    write_operator_approval_artifact,
    write_ptb_waiting_artifact,
)
from tyrex_pm.runtime.z_gap_boundary_gate import evaluate_boundary_ptb_gate
from tyrex_pm.runtime.z_gap_config_hash import compute_z_gap_config_fingerprint
from tyrex_pm.runtime.z_gap_live_preflight import validate_z_gap_live_scenario
from tyrex_pm.runtime.z_gap_preflight import load_z_gap_preflight_gates
from tyrex_pm.runtime.z_gap_session_orchestrator import (
    STATUS_NOT_READY,
    STATUS_PTB_READY,
    STATUS_READY_TO_WAIT,
    SessionDeps,
    ZGapSessionOrchestrator,
)
from tyrex_pm.strategies.z_gap.shadow_harness import ShadowHarness, TickSpec

_ALIGNED = 1_784_145_000


def _meta(start: float | None = None) -> Btc5mMarketMetadata:
    s = start if start is not None else float(_ALIGNED + BTC_5M_WINDOW_S)
    return Btc5mMarketMetadata(
        market_id="btc_5m_20260715_2000",
        condition_id="0xcond",
        yes_token_id="111",
        no_token_id="222",
        event_start_ts=s,
        event_end_ts=s + 300,
        event_slug=f"btc-updown-5m-{int(s)}",
        event_url=f"https://polymarket.com/event/btc-updown-5m-{int(s)}",
    )


def _risk() -> dict:
    return {
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
    }


def _runtime() -> dict:
    return {
        "execution_mode": "live",
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "market_data": {"enabled": True},
        "strategy_lifecycle": {"mode": "market_aware", "max_runtime_s": None},
        "signal_feeds": {"enabled": True},
        "external_btc": {"enabled": True},
        "reference_prices": {"enabled": True},
    }


def _strategy(**over) -> dict:
    zg = {
        "entry_mode": "enforce",
        "market_id": "btc_5m_<YYYYMMDD_HHMM>",
        "condition_id": "<required>",
        "yes_token_id": "<required>",
        "no_token_id": "<required>",
        "event_start_ts": None,
        "event_end_ts": None,
        "sizing": {"mode": "fixed_usd", "max_usd": "5", "min_shares": "5"},
        "exit": {
            "z_stop": "0.25",
            "stop_confirm_s": 1,
            "flatten_before_event_end_s": 20,
            "retry_interval_ms": 1000,
            "max_exit_attempts": 5,
        },
        "live_validation": {
            "target_prestart_seconds": 90,
            "hard_min_prestart_seconds": 20,
        },
        **over,
    }
    return {"kind": "z_gap", "enabled": True, "z_gap": zg}


def _write_static_artifacts(path: Path, meta: Btc5mMarketMetadata, *, market_id: str | None = None) -> None:
    mid = market_id or meta.market_id
    path.mkdir(parents=True, exist_ok=True)
    write_fee_curve_artifact(
        path / "fee_curve_spike.json",
        fee_model=FeeModel(
            fee_model_id="polymarket_dynamic_fd_v1",
            fee_model_status="resolved",
            fd_r=Decimal("0.07"),
            fd_e=Decimal("1"),
            fd_to=True,
        ),
        market_id=mid,
        condition_id=meta.condition_id,
        config_hash="test",
    )
    write_connectivity_artifact(
        path / "binance_connectivity.json",
        type("R", (), {"ok": True, "__dataclass_fields__": {}})(),
    )
    (path / "binance_connectivity.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (path / "clock_sanity.json").write_text(
        json.dumps(
            {
                "sync_status": "synced",
                "enforce_gate_pass": True,
                "time_authority_uncertainty_ms": 20.0,
            }
        ),
        encoding="utf-8",
    )
    write_ptb_waiting_artifact(path / "ptb_attestation.json", market_id=mid, event_start_ts=meta.event_start_ts)


@pytest.mark.asyncio
async def test_static_gates_pass_before_boundary_without_ptb(tmp_path: Path) -> None:
    meta = _meta(start=_ALIGNED + 600)
    art = tmp_path / "z_gap"
    _write_static_artifacts(art, meta)
    app = parse_app_config(risk=_risk(), strategy=_strategy(entry_mode="observe_only"), runtime=_runtime())
    app = replace(
        app,
        z_gap=replace(
            app.z_gap,
            entry_mode="enforce",
            market_id=meta.market_id,
            condition_id=meta.condition_id,
            yes_token_id=meta.yes_token_id,
            no_token_id=meta.no_token_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
        ),
    )
    fp = compute_z_gap_config_fingerprint(app)
    write_calibration_ack_artifact(art / "calibration_lite_review.json", fingerprint=fp, now_ts=_ALIGNED + 500, expiry_hours=168)
    write_operator_approval_artifact(
        art / "operator_enforce_approval.json",
        market_id=meta.market_id,
        condition_id=meta.condition_id,
        run_name="test",
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        now_ts=_ALIGNED + 500,
    )
    result = validate_z_gap_live_scenario(
        app,
        artifacts_dir=art,
        now_ts=_ALIGNED + 500,
        pre_boundary=True,
    )
    gates = load_z_gap_preflight_gates(art, phase="static", pre_boundary=True)
    assert result.ok
    assert gates.static_enforce_allowed
    assert gates.ptb_boundary_status == "WAITING_FOR_BOUNDARY"


@pytest.mark.asyncio
async def test_virtual_single_session_observe_no_order(tmp_path: Path) -> None:
    virtual_now = [_ALIGNED + 200.0]

    async def _sleep(dt: float) -> None:
        virtual_now[0] += dt

    meta = _meta(start=_ALIGNED + 600)
    art = tmp_path / "z_gap"
    _write_static_artifacts(art, meta)
    write_operator_approval_artifact(
        art / "operator_enforce_approval.json",
        market_id=meta.market_id,
        condition_id=meta.condition_id,
        run_name="shadow_session",
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        now_ts=virtual_now[0],
    )

    tick_path = tmp_path / "ticks.jsonl"
    from datetime import datetime, timezone

    tick_ts = datetime.fromtimestamp(meta.event_start_ts, tz=timezone.utc).isoformat()
    tick_path.write_text(
        json.dumps(
            {
                "source_ts": tick_ts,
                "recv_ts": tick_ts,
                "price": "100100.00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    from unittest.mock import patch
    from tyrex_pm.runtime.config import load_app_config

    orch = ZGapSessionOrchestrator(repo_root=Path(__file__).resolve().parents[1], artifacts_dir=art)
    orch.deps = SessionDeps(now_ts=lambda: virtual_now[0], sleep=_sleep, resolve_metadata=lambda _: meta)

    def _patched_load(**kwargs):
        with patch("tyrex_pm.runtime.z_gap_live.validate_z_gap_live_config"):
            app = load_app_config(
                repo_root=orch.repo_root,
                strategy_file=orch.strategy_file,
                scenario_file=orch.scenario_file,
            )
        mode = kwargs.get("entry_mode")
        if mode and app.z_gap is not None:
            app = replace(app, z_gap=replace(app.z_gap, entry_mode=mode))
        return app

    orch._load_app = _patched_load  # type: ignore[method-assign]
    orch.deps.sidecar = None

    fp = compute_z_gap_config_fingerprint(
        orch._load_app(entry_mode="observe_only"),
        strategy_file=orch.repo_root / orch.strategy_file,
        scenario_file=orch.repo_root / orch.scenario_file,
    )
    write_calibration_ack_artifact(
        art / "calibration_lite_review.json",
        fingerprint=fp,
        now_ts=virtual_now[0],
        expiry_hours=168,
    )

    def _boundary(**kwargs):
        kwargs["chainlink_log_path"] = tick_path
        kwargs["live_price"] = "100100.00"
        kwargs["live_status"] = "observed"
        kwargs["live_lag_ms"] = 50.0
        kwargs["artifacts_dir"] = art
        kwargs["ptb_store_path"] = tmp_path / "ptb_lock.json"
        kwargs["ptb_config_hash"] = fp.combined_hash
        return evaluate_boundary_ptb_gate(**kwargs)

    async def _binance():
        return type("R", (), {"ok": True})()

    async def _fee(app, meta):
        return FeeModel(
            fee_model_id="polymarket_dynamic_fd_v1",
            fee_model_status="resolved",
            fd_r=Decimal("0.07"),
            fd_e=Decimal("1"),
            fd_to=True,
        )

    orch.deps.run_binance_check = _binance
    orch.deps.run_clock_check = lambda: (
        type("R", (), {"to_dict": lambda self: {"sync_status": "synced", "enforce_gate_pass": True, "time_authority_uncertainty_ms": 20.0}})(),
        None,
    )
    orch.deps.resolve_fee_model = _fee
    orch.deps.boundary_evaluator = _boundary
    orch._approved_market_id = meta.market_id

    from unittest.mock import AsyncMock
    from tyrex_pm.core.enums import ExecutionMode
    from tyrex_pm.core.ids import TokenId
    from tyrex_pm.runtime.z_gap_session_runtime import (
        bootstrap_session_runtime,
        shutdown_session_runtime,
    )
    from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
    from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED, SignalStateStore
    from tyrex_pm.runtime.time_authority import TimeAuthority

    async def _patched_bootstrap(**kwargs):
        app = kwargs["app"]
        app = replace(app, runtime=replace(app.runtime, execution_mode=ExecutionMode.SHADOW))
        handle = await bootstrap_session_runtime(
            app=app,
            run_name=kwargs["run_name"],
            repo_root=kwargs["repo_root"],
            runs_dir=kwargs.get("runs_dir"),
        )
        ts = datetime.fromtimestamp(meta.event_start_ts, tz=timezone.utc)
        handle.coord.signal_state = SignalStateStore()
        store = handle.coord.signal_state
        store.update_binance(Decimal("100150"), source_ts=ts, recv_ts=ts, stream="bookTicker")
        store.update_chainlink(Decimal("100100"), source_ts=ts, recv_ts=ts)
        store.update_price_to_beat(Decimal("100100"), status=FRESHNESS_OBSERVED, observed_ts=ts, lag_ms=50.0)
        handle.coord.market_state = MarketStateStore(default_max_age_s=30.0)
        handle.coord.market_state.apply_snapshot(
            make_snapshot(TokenId("111"), bids=[(Decimal("0.6"), Decimal("100"))], asks=[(Decimal("0.61"), Decimal("100"))], ts=ts)
        )
        handle.coord.time_authority = TimeAuthority(
            sync_status="synced",
            offset_ms=0.0,
            uncertainty_ms=20.0,
            median_offset_ms=0.0,
            samples_requested=3,
            samples_kept=3,
            max_rtt_ms=50.0,
            _epoch_at_sync=virtual_now[0],
            _mono_at_sync=0.0,
        )
        return handle

    async def _patched_feeds(handle, _app):
        handle.feeds_ready = True

    async def _patched_warm(handle, _app, **kwargs):
        handle.sigma_warm = True
        return True

    from tyrex_pm.runtime import z_gap_session_runtime as rt

    real_observe = rt.run_z_gap_observe_loop

    async def _short_observe(**kw):
        kw["max_ticks"] = 1
        kw["tick_interval_s"] = 0.001
        kw["skip_late_start_check"] = True
        return await real_observe(**kw)

    with patch.object(rt, "run_z_gap_observe_loop", _short_observe):
        cert = {
            "policy_name": "z_gap_ptb_commissioning_v1",
            "policy_id": "z_gap_ptb_commissioning_v1",
            "status": "valid",
            "ptb_config_hash": fp.combined_hash,
            "issued_at_ts": virtual_now[0],
            "windows": [
                {"ptb_error_bps": 0.1, "usable": True},
                {"ptb_error_bps": 0.1, "usable": True},
                {"ptb_error_bps": 0.1, "usable": True},
            ],
        }
        (art / "ptb_commissioning_certificate.json").write_text(json.dumps(cert), encoding="utf-8")
        with patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.bootstrap_session_runtime",
            _patched_bootstrap,
        ):
            with patch(
                "tyrex_pm.runtime.z_gap_session_orchestrator.start_production_feeds",
                _patched_feeds,
            ):
                with patch(
                    "tyrex_pm.runtime.z_gap_session_orchestrator.warm_sigma_before_boundary",
                    _patched_warm,
                ):
                    result = await orch.run_session(
                        run_name="virtual_observe",
                        next_window=False,
                        event_url=meta.event_url,
                        execute=False,
                        interactive=False,
                        observe_only=True,
                    )
    assert result.status in {STATUS_PTB_READY, STATUS_READY_TO_WAIT, "TERMINAL", "NO_TRADE"}, result.blockers
    assert not result.order_submitted
    assert result.oms_submissions == 0


@pytest.mark.asyncio
async def test_non_interactive_never_executes(tmp_path: Path) -> None:
    virtual_now = [_ALIGNED + 200.0]

    async def _sleep(dt: float) -> None:
        virtual_now[0] += dt

    meta = _meta(start=_ALIGNED + 600)
    orch = ZGapSessionOrchestrator(repo_root=Path(__file__).resolve().parents[1], artifacts_dir=tmp_path / "z_gap")
    orch.deps = SessionDeps(now_ts=lambda: virtual_now[0], sleep=_sleep, resolve_metadata=lambda _: meta)
    orch._load_app = lambda **kwargs: parse_app_config(  # type: ignore[method-assign]
        risk=_risk(),
        strategy=_strategy(entry_mode=kwargs.get("entry_mode", "observe_only")),
        runtime=_runtime(),
    )
    result = await orch.run_session(
        run_name="ci_validate",
        next_window=False,
        event_url=meta.event_url,
        execute=False,
        interactive=False,
    )
    assert result.status == STATUS_NOT_READY


@pytest.mark.asyncio
async def test_shadow_single_session_orchestration(tmp_path: Path) -> None:
    from tyrex_pm.strategies.z_gap.scenario_oms import ScenarioOMS

    event_start = _ALIGNED + 600
    event_end = event_start + 300
    harness = ShadowHarness.create(
        tmp_path=tmp_path / "shadow",
        event_start_ts=event_start,
        event_end_ts=event_end,
        oms=ScenarioOMS(),
        exit_cfg={"stop_confirm_s": 0, "retry_interval_ms": 0, "z_stop": "0.05"},
    )
    result = await harness.run_ticks(
        [
            TickSpec(now_ts=event_start + 90, binance_price=Decimal("100150")),
            TickSpec(now_ts=event_start + 91, binance_price=Decimal("99800")),
        ]
    )
    assert result.exit_code in {0, 1}
    assert result.enforce_state.observe.loop_ran
