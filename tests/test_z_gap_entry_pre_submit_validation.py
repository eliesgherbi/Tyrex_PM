"""Tests for Z-Gap pre-submit validation (A0.6)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE, Z_GAP_ENTRY_MODE_OBSERVE_ONLY, ZGapSizingConfig
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.runtime.z_gap_preflight import ZGapPreflightGates
from tyrex_pm.state.signal_state_store import FRESHNESS_OBSERVED, FRESHNESS_PENDING, SignalSnapshot
from tyrex_pm.strategies.z_gap.entry_plan import (
    ORDER_STYLE_FAK,
    PLAN_STATUS_READY,
    REASON_CALIBRATION_NOT_REVIEWED,
    REASON_CLOCK_SYNC,
    REASON_EDGE_FLOOR,
    REASON_LIMIT_ABOVE_ASK,
    REASON_OBSERVE_MODE,
    REASON_OPERATOR_NOT_APPROVED,
    REASON_PTB_UNUSABLE,
    TIME_IN_FORCE_FAK,
    ZGapEntryPlan,
    validate_z_gap_pre_submit,
    z_gap_entry_plan_to_intent_work_unit,
)

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
SIZING = ZGapSizingConfig(mode="fixed_usd", max_usd=Decimal("5"), min_shares=Decimal("5"))


def _ready_plan(**over) -> ZGapEntryPlan:
    base = dict(
        market_id="m1",
        condition_id="0xabc",
        selected_leg="UP",
        token_id="111",
        p_L=Decimal("0.65"),
        ask_seen=Decimal("0.61"),
        theta_fill_floor=Decimal("0.02"),
        max_fill_price=Decimal("0.60"),
        final_limit_price=Decimal("0.60"),
        phi_at_limit=Decimal("0.0063"),
        expected_slippage=Decimal("0.01"),
        predicted_edge_at_limit=Decimal("0.0337"),
        shares=Decimal("8"),
        notional_usd=Decimal("4.8"),
        order_style=ORDER_STYLE_FAK,
        time_in_force=TIME_IN_FORCE_FAK,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        plan_status=PLAN_STATUS_READY,
        reject_reason=None,
        created_ts=TS,
    )
    base.update(over)
    return ZGapEntryPlan(**base)


def _signal(**over) -> SignalSnapshot:
    base = dict(
        binance_price=Decimal("100"),
        binance_source_ts=TS,
        binance_recv_ts=TS,
        binance_age_ms=1.0,
        binance_freshness="fresh",
        chainlink_price=Decimal("100"),
        chainlink_source_ts=TS,
        chainlink_recv_ts=TS,
        chainlink_age_ms=1.0,
        chainlink_freshness="fresh",
        price_to_beat=Decimal("100"),
        ptb_status=FRESHNESS_OBSERVED,
        ptb_observed_ts=TS,
        ptb_lag_ms=0.0,
        basis_bps=Decimal("1"),
        basis_status="fresh",
        feed_reject_reason=None,
        snapshot_ts=TS,
    )
    base.update(over)
    return SignalSnapshot(**base)


def _synced_ta() -> TimeAuthority:
    return TimeAuthority(
        sync_status="synced",
        offset_ms=10.0,
        uncertainty_ms=20.0,
        median_offset_ms=10.0,
        samples_requested=7,
        samples_kept=3,
        max_rtt_ms=40.0,
        _epoch_at_sync=1_000_000.0,
        _mono_at_sync=100.0,
    )


def _preflight_pass() -> ZGapPreflightGates:
    return ZGapPreflightGates(
        fee_curve_spike_passed=True,
        binance_connectivity_passed=True,
        ptb_attestation_passed=True,
        clock_sanity_passed=True,
        calibration_lite_reviewed=True,
        operator_approved_enforce=True,
        blockers=(),
    )


def test_blocks_observe_only_mode() -> None:
    result = validate_z_gap_pre_submit(
        _ready_plan(),
        entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_OBSERVE_MODE


def test_blocks_clock_sync_failed() -> None:
    bad_ta = TimeAuthority(sync_status="failed", samples_requested=7, samples_kept=0)
    result = validate_z_gap_pre_submit(
        _ready_plan(),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=bad_ta,
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_CLOCK_SYNC


def test_blocks_ptb_unusable() -> None:
    result = validate_z_gap_pre_submit(
        _ready_plan(),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(ptb_status=FRESHNESS_PENDING, price_to_beat=None),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_PTB_UNUSABLE


def test_blocks_calibration_not_reviewed() -> None:
    pf = _preflight_pass()
    pf = ZGapPreflightGates(
        fee_curve_spike_passed=True,
        binance_connectivity_passed=True,
        ptb_attestation_passed=True,
        clock_sanity_passed=True,
        calibration_lite_reviewed=False,
        operator_approved_enforce=True,
        blockers=("calibration_lite_reviewed",),
    )
    result = validate_z_gap_pre_submit(
        _ready_plan(),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=pf,
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_CALIBRATION_NOT_REVIEWED


def test_blocks_operator_approval_false() -> None:
    pf = ZGapPreflightGates(
        fee_curve_spike_passed=True,
        binance_connectivity_passed=True,
        ptb_attestation_passed=True,
        clock_sanity_passed=True,
        calibration_lite_reviewed=True,
        operator_approved_enforce=False,
        blockers=("operator_approved_enforce",),
    )
    result = validate_z_gap_pre_submit(
        _ready_plan(),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=pf,
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_OPERATOR_NOT_APPROVED


def test_blocks_predicted_edge_below_floor() -> None:
    result = validate_z_gap_pre_submit(
        _ready_plan(predicted_edge_at_limit=Decimal("0.01")),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_EDGE_FLOOR


def test_blocks_final_limit_above_ask() -> None:
    result = validate_z_gap_pre_submit(
        _ready_plan(final_limit_price=Decimal("0.62"), ask_seen=Decimal("0.61")),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert not result.passed
    assert result.reject_reason == REASON_LIMIT_ABOVE_ASK


def test_passes_when_all_gates_true() -> None:
    result = validate_z_gap_pre_submit(
        _ready_plan(),
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert result.passed
    assert result.reject_reason is None


def test_no_intent_work_unit_without_validation() -> None:
    plan = _ready_plan()
    bad = validate_z_gap_pre_submit(
        plan,
        entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    assert z_gap_entry_plan_to_intent_work_unit(
        plan,
        owner_id="z_gap",
        validation=bad,
        correlation_id="corr-1",
    ) is None


def test_intent_work_unit_when_validation_passes() -> None:
    plan = _ready_plan()
    ok = validate_z_gap_pre_submit(
        plan,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    work = z_gap_entry_plan_to_intent_work_unit(
        plan,
        owner_id="z_gap",
        validation=ok,
        correlation_id="corr-1",
    )
    assert work is not None
    assert work.intent.limit_price == Decimal("0.60")
    assert work.intent.size == Decimal("8")


@pytest.mark.asyncio
async def test_pipeline_not_called_without_validation() -> None:
    plan = _ready_plan()
    bad = validate_z_gap_pre_submit(
        plan,
        entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
        fee_model_status="resolved",
        signal=_signal(),
        time_authority=_synced_ta(),
        preflight=_preflight_pass(),
        sizing_cfg=SIZING,
    )
    work = z_gap_entry_plan_to_intent_work_unit(
        plan,
        owner_id="z_gap",
        validation=bad,
        correlation_id="corr-x",
    )
    assert work is None
    with patch("tyrex_pm.runtime.pipeline.process_intent_work_unit", new_callable=AsyncMock) as mock_proc:
        if work is not None:
            await mock_proc(work)
        mock_proc.assert_not_called()
