"""Experimental Z-Gap workflow tests (observe + tiny live)."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tyrex_pm.ingestion.btc_5m_window_scheduler import Btc5mWindowPlan
from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_LATE,
    PTB_STATUS_MISSING,
    PTB_STATUS_OBSERVED,
)
from tyrex_pm.runtime.btc_5m_metadata import Btc5mMarketMetadata
from tyrex_pm.runtime.config import (
    Z_GAP_ENTRY_MODE_ENFORCE,
    Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    load_app_config,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.z_gap_boundary_gate import (
    PTB_STATUS_MISSING_GATE,
    PTB_STATUS_READY,
    evaluate_boundary_ptb_gate,
)
from tyrex_pm.runtime.z_gap_experimental import (
    MAX_EXPERIMENTAL_USD,
    read_live_ptb_from_handle,
    validate_experimental_live_metadata,
)
from tyrex_pm.runtime.z_gap_session_orchestrator import (
    STATUS_NOT_READY,
    STATUS_TERMINAL,
    SessionDeps,
    ZGapSessionOrchestrator,
)
from tyrex_pm.runtime.z_gap_session_runtime import SessionRuntimeHandle
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.signal_state_store import SignalStateStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.z_gap.ptb_policy import select_ptb_source


def _meta(*, start: float | None = None) -> Btc5mMarketMetadata:
    now = time.time()
    s = start if start is not None else now + 120
    return Btc5mMarketMetadata(
        market_id="btc_5m_20260716_1200",
        condition_id="0xabc",
        yes_token_id="111",
        no_token_id="222",
        event_start_ts=s,
        event_end_ts=s + 300,
        event_slug="btc-updown-5m-test",
        event_url="https://polymarket.com/event/btc-updown-5m-test",
    )


def _plan(meta: Btc5mMarketMetadata) -> Btc5mWindowPlan:
    return Btc5mWindowPlan(
        window_start_ts=int(meta.event_start_ts),
        window_end_ts=int(meta.event_end_ts),
        event_slug=meta.event_slug,
        event_url=meta.event_url or "",
        wake_at_ts=time.time(),
    )


def _runtime_handle_with_ptb(
    *,
    price: str | None = "95000.12",
    status: str = PTB_STATUS_OBSERVED,
    lag_ms: float = 100.0,
) -> SessionRuntimeHandle:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    store = SignalStateStore()
    if price is not None:
        store.update_price_to_beat(
            price=Decimal(price),
            status=status,
            observed_ts=datetime.now(timezone.utc),
            lag_ms=lag_ms,
        )
    coord.signal_state = store
    return SessionRuntimeHandle(
        repo_root=Path("."),
        runs_dir=Path("."),
        run_id=MagicMock(),
        coord=coord,
        sink=MagicMock(),
        stop=MagicMock(),
        oms=MagicMock(submit_count=0),
    )


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    (tmp_path / "config" / "scenarios").mkdir(parents=True)
    (tmp_path / "config" / "strategies").mkdir(parents=True)
    (tmp_path / "config" / "scenarios" / "live_z_gap_tiny.yaml").write_text(
        "scenario: z_gap_tiny\n", encoding="utf-8"
    )
    (tmp_path / "config" / "strategies" / "z_gap.yaml").write_text(
        "strategy: z_gap\n", encoding="utf-8"
    )
    return tmp_path


def _ptb_capture_mock(boundary: Any, handle: SessionRuntimeHandle, meta: Btc5mMarketMetadata) -> Any:
    from tyrex_pm.runtime.z_gap_experimental import read_live_ptb_from_handle
    from tyrex_pm.runtime.z_gap_ptb_capture import build_ptb_capture_trace

    reading = read_live_ptb_from_handle(handle)
    now = time.time()
    trace = build_ptb_capture_trace(
        meta=meta,
        boundary=boundary,
        ptb_reading=reading,
        capture_started_at=now,
        capture_deadline=now,
        capture_ended_at=now,
        poll_count=1,
        max_lag_ms=5000.0,
    )
    return AsyncMock(return_value=(boundary, trace, reading))


def _make_orchestrator(repo_root: Path, *, artifacts: Path) -> ZGapSessionOrchestrator:
    return ZGapSessionOrchestrator(
        repo_root=repo_root,
        artifacts_dir=artifacts,
        deps=SessionDeps(
            now_ts=lambda: time.time(),
            sleep=AsyncMock(),
            write_fn=lambda _m: None,
        ),
    )


@pytest.mark.asyncio
async def test_observe_skips_production_preflight_blockers(repo_root: Path, tmp_path: Path) -> None:
    """Observe starts without calibration, operator approval, ETH RPC, cert, or private key."""
    art = tmp_path / "z_gap"
    orch = _make_orchestrator(repo_root, artifacts=art)
    meta = _meta()
    plan = _plan(meta)

    orch.discover_next_window = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(plan=plan, skipped=(), metadata=meta)
    )
    orch.prepare_static_artifacts = AsyncMock(return_value=([], ["fee_curve: not generated"]))  # type: ignore[method-assign]
    orch.validate_static_readiness = MagicMock(  # type: ignore[method-assign]
        return_value=(False, ("calibration_lite_review.json missing",), ())
    )
    orch.wait_for_boundary = AsyncMock()  # type: ignore[method-assign]

    boundary = evaluate_boundary_ptb_gate(
        market_id=meta.market_id,
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        now_ts=time.time(),
        live_price=None,
        live_status=PTB_STATUS_MISSING,
        experimental_mode=True,
    )
    orch.evaluate_boundary = AsyncMock(return_value=boundary)  # type: ignore[method-assign]

    tracking_oms = MagicMock(submit_count=0)
    handle = _runtime_handle_with_ptb(price=None, status=PTB_STATUS_MISSING)
    handle.oms = tracking_oms
    cap_mock = _ptb_capture_mock(boundary, handle, meta)

    with (
        patch.object(orch, "_load_app") as load_app,
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.capture_ptb_at_boundary",
            cap_mock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.bootstrap_session_runtime",
            new_callable=AsyncMock,
            return_value=handle,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.start_production_feeds",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.warm_sigma_before_boundary",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.run_post_boundary_runtime",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.shutdown_session_runtime",
            new_callable=AsyncMock,
        ),
        patch.dict("os.environ", {}, clear=False),
    ):
        app = parse_app_config(
            risk={"notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"}},
            strategy={
                "kind": "z_gap",
                "enabled": True,
                "z_gap": {
                    "entry_mode": "observe_only",
                    "market_id": meta.market_id,
                    "condition_id": meta.condition_id,
                    "yes_token_id": meta.yes_token_id,
                    "no_token_id": meta.no_token_id,
                    "event_start_ts": meta.event_start_ts,
                    "event_end_ts": meta.event_end_ts,
                    "sizing": {"mode": "fixed_usd", "max_usd": "5"},
                    "live_validation": {
                        "target_prestart_seconds": 60,
                        "hard_min_prestart_seconds": 20,
                    },
                },
            },
            runtime={"execution_mode": "live", "reporting": {"enabled": True, "runs_dir": "var/runs"}},
        )
        load_app.return_value = app
        result = await orch.run_session(
            run_name="observe_smoke",
            observe_only=True,
            interactive=False,
        )

    assert result.status == STATUS_TERMINAL
    assert result.oms_submissions == 0
    assert not any("calibration" in b for b in result.blockers)
    assert not any("operator" in b for b in result.blockers)
    orch.validate_static_readiness.assert_not_called()


@pytest.mark.asyncio
async def test_observe_uses_shadow_oms_not_live(repo_root: Path, tmp_path: Path) -> None:
    art = tmp_path / "z_gap"
    orch = _make_orchestrator(repo_root, artifacts=art)
    meta = _meta()
    orch.discover_next_window = MagicMock(return_value=MagicMock(plan=_plan(meta), skipped=(), metadata=meta))  # type: ignore[method-assign]
    orch.prepare_static_artifacts = AsyncMock(return_value=([], []))  # type: ignore[method-assign]
    orch.wait_for_boundary = AsyncMock()  # type: ignore[method-assign]
    orch.evaluate_boundary = AsyncMock(  # type: ignore[method-assign]
        return_value=evaluate_boundary_ptb_gate(
            market_id=meta.market_id,
            event_start_ts=meta.event_start_ts,
            event_end_ts=meta.event_end_ts,
            now_ts=time.time(),
            experimental_mode=True,
        )
    )

    with (
        patch.object(orch, "_load_app") as load_app,
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.capture_ptb_at_boundary",
            _ptb_capture_mock(
                evaluate_boundary_ptb_gate(
                    market_id=meta.market_id,
                    event_start_ts=meta.event_start_ts,
                    event_end_ts=meta.event_end_ts,
                    now_ts=time.time(),
                    experimental_mode=True,
                ),
                _runtime_handle_with_ptb(),
                meta,
            ),
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.bootstrap_session_runtime",
            new_callable=AsyncMock,
        ) as bootstrap,
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.start_production_feeds",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.warm_sigma_before_boundary",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.run_post_boundary_runtime",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.shutdown_session_runtime",
            new_callable=AsyncMock,
        ),
    ):
        app = parse_app_config(
            risk={"notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"}},
            strategy={
                "kind": "z_gap",
                "enabled": True,
                "z_gap": {
                    "entry_mode": "observe_only",
                    "market_id": meta.market_id,
                    "condition_id": meta.condition_id,
                    "yes_token_id": meta.yes_token_id,
                    "no_token_id": meta.no_token_id,
                    "event_start_ts": meta.event_start_ts,
                    "event_end_ts": meta.event_end_ts,
                    "live_validation": {"target_prestart_seconds": 60, "hard_min_prestart_seconds": 20},
                },
            },
            runtime={"execution_mode": "live", "reporting": {"enabled": True, "runs_dir": "var/runs"}},
        )
        load_app.return_value = app
        bootstrap.return_value = _runtime_handle_with_ptb()
        await orch.run_session(run_name="shadow_oms", observe_only=True, interactive=False)

    bootstrap.assert_awaited_once()
    assert bootstrap.await_args.kwargs.get("use_shadow_oms") is True


def test_read_live_ptb_from_signal_state_store() -> None:
    handle = _runtime_handle_with_ptb(price="95123.45", lag_ms=250.0)
    reading = read_live_ptb_from_handle(handle)
    assert reading.price == "95123.45"
    assert reading.status == PTB_STATUS_OBSERVED
    assert reading.lag_ms == 250.0


def test_boundary_experimental_missing_k_reports_not_blocks(tmp_path: Path) -> None:
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20260716_1203",
        event_start_ts=time.time(),
        event_end_ts=time.time() + 300,
        now_ts=time.time(),
        live_price=None,
        live_status=PTB_STATUS_MISSING,
        chainlink_log_path=tmp_path / "no_ticks.jsonl",
        ptb_store_path=tmp_path / "ptb_missing.json",
        artifacts_dir=tmp_path / "art_missing",
        experimental_mode=True,
    )
    assert result.status == PTB_STATUS_MISSING_GATE
    assert not result.ready_to_evaluate
    assert (tmp_path / "art_missing" / "ptb_boundary_lock.json").is_file()


def test_boundary_experimental_late_k_reports(tmp_path: Path) -> None:
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20260716_1201",
        event_start_ts=time.time() - 60,
        event_end_ts=time.time() + 240,
        now_ts=time.time(),
        live_price=None,
        live_status=PTB_STATUS_LATE,
        live_lag_ms=9000.0,
        chainlink_log_path=tmp_path / "missing_ticks.jsonl",
        ptb_store_path=tmp_path / "ptb_store.json",
        artifacts_dir=tmp_path / "art_late",
        experimental_mode=True,
    )
    assert not result.ready_to_evaluate
    assert result.status in {"PTB_LATE", "PTB_MISSING"}


def test_boundary_experimental_locks_usable_k(tmp_path: Path) -> None:
    result = evaluate_boundary_ptb_gate(
        market_id="btc_5m_20260716_1202",
        event_start_ts=time.time(),
        event_end_ts=time.time() + 300,
        now_ts=time.time(),
        live_price="95000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=100.0,
        chainlink_log_path=tmp_path / "missing_ticks.jsonl",
        ptb_store_path=tmp_path / "ptb_store_lock.json",
        artifacts_dir=tmp_path / "art_lock",
        experimental_mode=True,
    )
    assert result.ready_to_evaluate
    assert result.status == PTB_STATUS_READY
    assert result.selection is not None
    assert result.selection.locked
    lock = json.loads((tmp_path / "art_lock" / "ptb_boundary_lock.json").read_text(encoding="utf-8"))
    assert lock["selected_k"] == "95000.00"


def test_experimental_mismatch_reports_but_keeps_primary() -> None:
    sel = select_ptb_source(
        market_id="btc_5m_20260716_1200",
        event_start_ts=time.time(),
        event_end_ts=time.time() + 300,
        live_price="95000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=100.0,
        log_derivation=type(
            "D",
            (),
            {
                "price": "94900.00",
                "status": PTB_STATUS_OBSERVED,
                "boundary_lag_ms": 100.0,
            },
        )(),
        experimental_mode=True,
    )
    assert sel.mismatch
    assert sel.selected_k == "95000.00"
    assert sel.usable


def test_experimental_live_load_app_skips_production_preflight() -> None:
    repo = Path(__file__).resolve().parents[1]
    orch = ZGapSessionOrchestrator(
        repo_root=repo,
        scenario_file="config/scenarios/live_z_gap_tiny.yaml",
        artifacts_dir=repo / "var" / "reporting" / "z_gap",
    )
    app = orch._load_app(entry_mode=Z_GAP_ENTRY_MODE_ENFORCE, experimental_mode=True)
    assert app.z_gap is not None
    assert app.z_gap.entry_mode == Z_GAP_ENTRY_MODE_ENFORCE


def test_experimental_live_max_usd_rejected() -> None:
    zg = parse_app_config(
        risk={"notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"}},
        strategy={
            "kind": "z_gap",
            "enabled": True,
            "z_gap": {
                "entry_mode": "observe_only",
                "market_id": "btc_5m_20260716_1200",
                "condition_id": "0x",
                "yes_token_id": "1",
                "no_token_id": "2",
                "event_start_ts": time.time() + 60,
                "event_end_ts": time.time() + 360,
            },
        },
        runtime={"execution_mode": "live"},
    ).z_gap
    assert zg is not None
    assert validate_experimental_live_metadata(zg, max_usd=Decimal("6"))
    assert validate_experimental_live_metadata(zg, max_usd=MAX_EXPERIMENTAL_USD) == []


def test_experimental_live_cli_max_usd_parser() -> None:
    from scripts.go_z_gap_tiny_live import _build_parser
    from decimal import Decimal

    args = _build_parser().parse_args(
        ["--next-window", "--run-name", "x", "--experimental-live", "--max-usd", "6"]
    )
    assert Decimal(str(args.max_usd)) > Decimal("5")


@pytest.mark.asyncio
async def test_observe_missing_k_reaches_terminal_report(repo_root: Path, tmp_path: Path) -> None:
    art = tmp_path / "z_gap"
    orch = _make_orchestrator(repo_root, artifacts=art)
    meta = _meta()
    orch.discover_next_window = MagicMock(return_value=MagicMock(plan=_plan(meta), skipped=(), metadata=meta))  # type: ignore[method-assign]
    orch.prepare_static_artifacts = AsyncMock(return_value=([], []))  # type: ignore[method-assign]
    orch.wait_for_boundary = AsyncMock()  # type: ignore[method-assign]
    boundary = evaluate_boundary_ptb_gate(
        market_id=meta.market_id,
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        now_ts=time.time(),
        live_price=None,
        live_status=PTB_STATUS_MISSING,
        chainlink_log_path=tmp_path / "no_ticks.jsonl",
        ptb_store_path=tmp_path / "ptb_missing_observe.json",
        artifacts_dir=art,
        experimental_mode=True,
    )
    orch.evaluate_boundary = AsyncMock(return_value=boundary)  # type: ignore[method-assign]
    handle = _runtime_handle_with_ptb(price=None, status=PTB_STATUS_MISSING)
    cap_mock = _ptb_capture_mock(boundary, handle, meta)

    with (
        patch.object(orch, "_load_app") as load_app,
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.capture_ptb_at_boundary",
            cap_mock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.bootstrap_session_runtime",
            new_callable=AsyncMock,
            return_value=handle,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.start_production_feeds",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.warm_sigma_before_boundary",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.run_post_boundary_runtime",
            new_callable=AsyncMock,
            return_value=0,
        ) as post_runtime,
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.shutdown_session_runtime",
            new_callable=AsyncMock,
        ),
    ):
        app = parse_app_config(
            risk={"notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"}},
            strategy={
                "kind": "z_gap",
                "enabled": True,
                "z_gap": {
                    "entry_mode": "observe_only",
                    "market_id": meta.market_id,
                    "condition_id": meta.condition_id,
                    "yes_token_id": meta.yes_token_id,
                    "no_token_id": meta.no_token_id,
                    "event_start_ts": meta.event_start_ts,
                    "event_end_ts": meta.event_end_ts,
                    "live_validation": {"target_prestart_seconds": 60, "hard_min_prestart_seconds": 20},
                },
            },
            runtime={"execution_mode": "live", "reporting": {"enabled": True, "runs_dir": "var/runs"}},
        )
        load_app.return_value = app
        result = await orch.run_session(run_name="missing_k", observe_only=True, interactive=False)

    assert result.status == STATUS_TERMINAL
    post_runtime.assert_awaited_once()
    assert result.report_path
    report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
    assert report["ptb_usable"] is False
    assert report["oms_submissions"] == 0


@pytest.mark.asyncio
async def test_single_window_no_auto_continue(repo_root: Path, tmp_path: Path) -> None:
    """Experimental session stops after one window — no run_continue."""
    art = tmp_path / "z_gap"
    orch = _make_orchestrator(repo_root, artifacts=art)
    meta = _meta()
    orch.discover_next_window = MagicMock(return_value=MagicMock(plan=_plan(meta), skipped=(), metadata=meta))  # type: ignore[method-assign]
    orch.prepare_static_artifacts = AsyncMock(return_value=([], []))  # type: ignore[method-assign]
    orch.wait_for_boundary = AsyncMock()  # type: ignore[method-assign]
    boundary_result = evaluate_boundary_ptb_gate(
        market_id=meta.market_id,
        event_start_ts=meta.event_start_ts,
        event_end_ts=meta.event_end_ts,
        now_ts=time.time(),
        live_price="95000",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=50.0,
        experimental_mode=True,
    )
    orch.evaluate_boundary = AsyncMock(return_value=boundary_result)  # type: ignore[method-assign]
    handle = _runtime_handle_with_ptb()

    with (
        patch.object(orch, "_load_app") as load_app,
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.capture_ptb_at_boundary",
            _ptb_capture_mock(boundary_result, handle, meta),
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.bootstrap_session_runtime",
            new_callable=AsyncMock,
            return_value=handle,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.start_production_feeds",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.warm_sigma_before_boundary",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.run_post_boundary_runtime",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.shutdown_session_runtime",
            new_callable=AsyncMock,
        ),
        patch(
            "tyrex_pm.runtime.z_gap_session_orchestrator.prompt_experimental_live_approval",
            return_value=True,
        ),
        patch("tyrex_pm.runtime.z_gap_live.validate_z_gap_live_config"),
    ):
        app = parse_app_config(
            risk={"notional": {"min_usd": "0.01", "max_usd": "5", "max_policy": "deny"}},
            strategy={
                "kind": "z_gap",
                "enabled": True,
                "z_gap": {
                    "entry_mode": "enforce",
                    "market_id": meta.market_id,
                    "condition_id": meta.condition_id,
                    "yes_token_id": meta.yes_token_id,
                    "no_token_id": meta.no_token_id,
                    "event_start_ts": meta.event_start_ts,
                    "event_end_ts": meta.event_end_ts,
                    "sizing": {"mode": "fixed_usd", "max_usd": "5", "min_shares": "5"},
                    "entry": {"one_position_per_window": True, "no_reentry_after_exit": True},
                    "exit": {
                        "flatten_before_event_end_s": 20,
                        "retry_interval_ms": 1000,
                        "max_exit_attempts": 3,
                    },
                    "live_validation": {"target_prestart_seconds": 60, "hard_min_prestart_seconds": 20},
                },
            },
            runtime={"execution_mode": "live", "reporting": {"enabled": True, "runs_dir": "var/runs"}},
        )
        load_app.return_value = app
        result = await orch.run_session(
            run_name="one_window",
            experimental_live=True,
            max_usd=Decimal("5"),
            interactive=True,
        )

    assert result.status == STATUS_TERMINAL
    orch.discover_next_window.assert_called_once()
