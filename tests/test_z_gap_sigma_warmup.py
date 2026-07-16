"""Tests for Z-Gap sigma warm-up and window selection lead time."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from tyrex_pm.ingestion.btc_5m_window_scheduler import select_btc_5m_session_window
from tyrex_pm.quant.volatility import EwmaVolatilityEstimator, SigmaConfig
from tyrex_pm.runtime.config import ZGapSigmaConfig
from tyrex_pm.runtime.z_gap_experimental import build_observe_session_report
from tyrex_pm.runtime.config import _parse_z_gap_entry
from tyrex_pm.runtime.z_gap_session_runtime import SessionRuntimeHandle
from tyrex_pm.runtime.z_gap_sigma_warmup import (
    PROVENANCE_SEEDED_THEN_LIVE,
    SIGMA_NOT_READY,
    SIGMA_WARM,
    sigma_warmup_required_seconds,
    warm_sigma_before_boundary,
)
from tyrex_pm.strategies.z_gap.entry_eval import REASON_SIGMA_NOT_READY, evaluate_z_gap_entry
from tyrex_pm.strategies.z_gap.shadow_harness import base_shadow_app

UTC = timezone.utc
_ALIGNED = 1_784_145_000


def _ts(sec: float) -> datetime:
    return datetime(2026, 7, 16, 8, 0, 0, tzinfo=UTC) + timedelta(seconds=sec)


def _make_prices(n: int, *, start: float = 100.0, step: float = 0.02) -> list[tuple[Decimal, datetime]]:
    return [(Decimal(str(start + step * i)), _ts(float(i))) for i in range(n)]


def test_sigma_warmup_required_seconds_uses_min_samples_and_feed_reserve() -> None:
    sigma = ZGapSigmaConfig(min_samples_s=20, sample_interval_s=1)
    assert sigma_warmup_required_seconds(sigma) == pytest.approx(46.0)


def test_window_with_sigma_lead_selects_when_sufficient() -> None:
    now = _ALIGNED
    plan, skipped = select_btc_5m_session_window(
        now_ts=now,
        target_prestart_seconds=90.0,
        hard_min_prestart_seconds=20.0,
        min_sigma_warmup_seconds=46.0,
    )
    assert not skipped
    assert plan.window_start_ts == _ALIGNED + 300


def test_insufficient_sigma_lead_skips_to_following_window() -> None:
    now = _ALIGNED + 264  # 36s before +300, below sigma requirement 46s
    plan, skipped = select_btc_5m_session_window(
        now_ts=now,
        target_prestart_seconds=90.0,
        hard_min_prestart_seconds=20.0,
        min_sigma_warmup_seconds=46.0,
    )
    assert skipped
    assert skipped[0].reason == "insufficient_sigma_warmup_lead"
    assert skipped[0].lead_time_s == pytest.approx(36.0)
    assert plan.window_start_ts == _ALIGNED + 600


def test_seed_uses_existing_ewma_estimator() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=5, sample_interval_s=1, jump_guard=False))
    result = est.seed_observations(_make_prices(8))
    snap = est.snapshot()
    assert result.accepted == 8
    assert snap.sample_count == 7
    assert snap.effective_samples_s >= 5
    assert snap.ready is True


def test_seed_rejects_duplicate_and_out_of_order_samples() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2, sample_interval_s=1, jump_guard=False))
    est.update(Decimal("100"), _ts(5))
    result = est.seed_observations([(Decimal("101"), _ts(2)), (Decimal("102"), _ts(3))])
    assert result.rejected_out_of_order == 2
    assert result.accepted == 0

    fresh = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=2, sample_interval_s=1, jump_guard=False))
    dup = fresh.seed_observations([(Decimal("100"), _ts(0)), (Decimal("100.1"), _ts(0))])
    assert dup.rejected_duplicate == 1
    assert dup.accepted == 1


def test_seed_rejects_future_samples() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=1, sample_interval_s=1, jump_guard=False))
    now = _ts(5)
    obs = [(Decimal("100"), _ts(0)), (Decimal("101"), _ts(10))]
    result = est.seed_observations(obs, now_ts=now)
    assert result.rejected_future == 1
    assert result.accepted == 1


def test_live_updates_continue_after_seeding() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=3, sample_interval_s=1, jump_guard=False))
    est.seed_observations(_make_prices(4))
    before = est.snapshot().sample_count
    est.update(Decimal("100.5"), _ts(10))
    after = est.snapshot()
    assert after.sample_count == before + 1
    assert after.ready is True


def test_seeded_sigma_reaches_min_samples_before_boundary() -> None:
    est = EwmaVolatilityEstimator(SigmaConfig(min_samples_s=20, sample_interval_s=1, jump_guard=False))
    est.seed_observations(_make_prices(25))
    snap = est.snapshot()
    assert snap.ready is True
    assert snap.effective_samples_s >= 20


def test_observe_report_includes_sigma_fields_when_unavailable() -> None:
    from pathlib import Path
    from unittest.mock import MagicMock

    from tyrex_pm.core.ids import RunId
    from tyrex_pm.runtime.coordinator import RuntimeCoordinator
    from tyrex_pm.runtime.health_runtime import HealthRuntime
    from tyrex_pm.state.order_store import OrderStore
    from tyrex_pm.state.wallet_store import WalletStore

    handle = SessionRuntimeHandle(
        repo_root=Path("."),
        runs_dir=Path("."),
        run_id=RunId("r1"),
        coord=RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime()),
        sink=MagicMock(),
        stop=asyncio.Event(),
    )
    handle.sigma_warmup.status = SIGMA_NOT_READY
    report = build_observe_session_report(
        meta=MagicMock(
            market_id="btc_5m_test",
            event_slug="btc-updown-5m-1",
            event_start_ts=1.0,
            event_end_ts=2.0,
        ),
        run_name="observe",
        prestart_s=90.0,
        boundary=MagicMock(),
        ptb_reading=MagicMock(),
        handle=handle,
        warnings=[],
        oms_submissions=0,
        terminal_status="TERMINAL",
        facts_path=None,
    )
    assert report["sigma_status"] == SIGMA_NOT_READY
    assert report["sigma_not_ready"] is True


def test_experimental_live_blocks_entry_when_sigma_not_ready() -> None:
    from test_z_gap_entry_eval import ENTRY, FEE_MODEL, _books, _edge, _fair, _signal, _vol
    from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE
    from tyrex_pm.runtime.time_authority import SYNC_STATUS_SYNCED, TimeAuthority

    ta = TimeAuthority(sync_status=SYNC_STATUS_SYNCED, samples_requested=5, samples_kept=3, uncertainty_ms=50.0)
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(ready=False, reject_reason="min_samples_not_met"),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        time_authority=ta,
    )
    assert ev.reason_code == REASON_SIGMA_NOT_READY


def test_strategy_sigma_parameters_unchanged() -> None:
    from pathlib import Path

    import yaml

    raw = yaml.safe_load((Path("config/strategies/z_gap.yaml")).read_text(encoding="utf-8"))
    sigma = raw["z_gap"]["sigma"]
    assert sigma["estimator"] == "ewma"
    assert sigma["half_life_s"] == 30
    assert sigma["min_samples_s"] == 20
    assert sigma["sample_interval_s"] == 1
    assert sigma["jump_guard"] is True
    assert sigma["jump_threshold_sigma"] == 4.0


@pytest.mark.asyncio
async def test_warm_sigma_emits_seeded_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace
    from pathlib import Path
    import tempfile

    from tyrex_pm.core.ids import RunId
    from tyrex_pm.reporting.sinks.jsonl import JsonlSink
    from tyrex_pm.runtime.coordinator import RuntimeCoordinator
    from tyrex_pm.runtime.health_runtime import HealthRuntime
    from tyrex_pm.runtime.time_authority import SYNC_STATUS_SYNCED, TimeAuthority
    from tyrex_pm.state.order_store import OrderStore
    from tyrex_pm.state.wallet_store import WalletStore

    monkeypatch.setattr("tyrex_pm.runtime.z_gap_live.validate_z_gap_live_config", lambda app: None)
    app = base_shadow_app(
        event_start_ts=9999999999.0,
        event_end_ts=9999999999.0 + 300,
    )
    app = replace(app, z_gap=replace(app.z_gap, entry_mode="observe_only"))
    assert app.z_gap is not None

    with tempfile.TemporaryDirectory() as tmp:
        facts_path = Path(tmp) / "facts.jsonl"
        sink = JsonlSink(facts_path)
        sink.__enter__()
        coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
        coord.time_authority = TimeAuthority(sync_status=SYNC_STATUS_SYNCED, samples_requested=5, samples_kept=3)
        handle = SessionRuntimeHandle(
            repo_root=Path(tmp),
            runs_dir=Path(tmp),
            run_id=RunId("warm-test"),
            coord=coord,
            sink=sink,
            stop=asyncio.Event(),
        )

        async def _fake_seed(est: EwmaVolatilityEstimator, **kwargs: object) -> tuple[object, list, str]:
            from tyrex_pm.quant.volatility import SeedResult

            est.seed_observations(_make_prices(25))
            return (
                SeedResult(
                    accepted=25,
                    rejected_duplicate=0,
                    rejected_future=0,
                    rejected_out_of_order=0,
                    start_ts=_ts(0),
                    end_ts=_ts(24),
                ),
                _make_prices(25),
                "aggTrade",
            )

        monkeypatch.setattr(
            "tyrex_pm.runtime.z_gap_sigma_warmup.seed_sigma_from_binance_history",
            AsyncMock(side_effect=_fake_seed),
        )
        monkeypatch.setattr("tyrex_pm.runtime.z_gap_sigma_warmup.run_observe_tick", lambda *a, **k: None)

        ready = await warm_sigma_before_boundary(handle, app, tick_interval_s=0.01)
        assert ready is True
        assert handle.sigma_warmup.provenance == PROVENANCE_SEEDED_THEN_LIVE
        assert handle.sigma_warmup.status == SIGMA_WARM
        assert handle.sigma_warmup.seed_sample_count == 25
        sink.__exit__(None, None, None)
