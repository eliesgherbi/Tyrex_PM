"""Tests for Z-Gap entry gate evaluation (A0.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot, QUALITY_OK
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import EDGE_STATUS_READY, compute_edge
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_ENFORCE, Z_GAP_ENTRY_MODE_OBSERVE_ONLY, _parse_z_gap_entry
from tyrex_pm.runtime.time_authority import TimeAuthority
from tyrex_pm.state.signal_state_store import (
    BASIS_FRESH,
    BASIS_UNTRUSTED,
    FRESHNESS_FRESH,
    FRESHNESS_LATE,
    FRESHNESS_MISSING,
    FRESHNESS_OBSERVED,
    FRESHNESS_PENDING,
    FRESHNESS_STALE,
    FRESHNESS_UNTRUSTED,
    SignalSnapshot,
)
from tyrex_pm.strategies.z_gap.entry_eval import (
    DECISION_NOT_READY,
    DECISION_SKIP,
    DECISION_WOULD_ENTER,
    REASON_BASIS_EXCEEDED,
    REASON_BOOK_STALE,
    REASON_CHAINLINK_STALE,
    REASON_CLOCK_SYNC_FAILED,
    REASON_EDGE_BELOW_THETA,
    REASON_FEED_STALE,
    REASON_FEE_MODEL_UNKNOWN,
    REASON_JUMP_GUARD,
    REASON_PTB_LATE,
    REASON_PTB_MISSING,
    REASON_PTB_MISMATCH,
    REASON_PTB_UNVERIFIED,
    REASON_SIGMA_NOT_READY,
    REASON_TAU_OUT_OF_BAND,
    REASON_Z_OUT_OF_BAND,
    evaluate_z_gap_entry,
)

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
ENTRY = _parse_z_gap_entry({})
FD_RAW = {"fd": {"r": 0.07, "e": 1, "to": True}}
FEE_MODEL = parse_fee_model_from_raw(FD_RAW)


def _signal(**over) -> SignalSnapshot:
    base = dict(
        binance_price=Decimal("100100"),
        binance_source_ts=TS,
        binance_recv_ts=TS,
        binance_age_ms=100.0,
        binance_freshness=FRESHNESS_FRESH,
        chainlink_price=Decimal("100000"),
        chainlink_source_ts=TS,
        chainlink_recv_ts=TS,
        chainlink_age_ms=100.0,
        chainlink_freshness=FRESHNESS_FRESH,
        price_to_beat=Decimal("100000"),
        ptb_status=FRESHNESS_OBSERVED,
        ptb_observed_ts=TS,
        ptb_lag_ms=500.0,
        basis_bps=Decimal("1"),
        basis_status=BASIS_FRESH,
        feed_reject_reason=None,
        snapshot_ts=TS,
    )
    base.update(over)
    return SignalSnapshot(**base)


def _fair(**over) -> FairValueSnapshot:
    base = dict(
        S=Decimal("100100"),
        K=Decimal("100000"),
        tau_s=120.0,
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        z=1.2,
        p_up=0.65,
        p_down=0.35,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )
    base.update(over)
    return FairValueSnapshot(**base)


def _vol(**over) -> VolatilitySnapshot:
    base = dict(
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=30,
        effective_samples_s=25.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )
    base.update(over)
    return VolatilitySnapshot(**base)


def _books(**over) -> PairBookSnapshot:
    up = LegBookQuote(
        token_id="111",
        bid=Decimal("0.60"),
        ask=Decimal("0.61"),
        stale=False,
        book_age_ms=100,
        spread=Decimal("0.01"),
        quality_status=QUALITY_OK,
    )
    down = LegBookQuote(
        token_id="222",
        bid=Decimal("0.38"),
        ask=Decimal("0.39"),
        stale=False,
        book_age_ms=100,
        spread=Decimal("0.01"),
        quality_status=QUALITY_OK,
    )
    return PairBookSnapshot(up=up, down=down)


def _edge(fair: FairValueSnapshot):
    return compute_edge(
        fair,
        ask_up=Decimal("0.61"),
        ask_down=Decimal("0.39"),
        fee_model=FEE_MODEL,
        expected_slippage_up=Decimal("0.01"),
        expected_slippage_down=Decimal("0.01"),
    )


def test_ptb_missing() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(price_to_beat=None, ptb_status=FRESHNESS_PENDING),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.decision_status == DECISION_SKIP
    assert ev.reason_code == REASON_PTB_MISSING


def test_ptb_late() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(ptb_status=FRESHNESS_LATE, ptb_lag_ms=3000),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_PTB_LATE


def test_ptb_unverified() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(ptb_status=FRESHNESS_UNTRUSTED),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_PTB_UNVERIFIED


def test_ptb_mismatch() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(price_to_beat=Decimal("100000")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        ptb_reference_k=Decimal("101000"),
    )
    assert ev.reason_code == REASON_PTB_MISMATCH


def test_observe_mode_ignores_raw_os_drift() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        clock_drift_ms=900.0,
        entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    )
    assert ev.gate_results.get("clock_sync") == "pass"
    assert ev.reason_code != REASON_CLOCK_SYNC_FAILED


def test_enforce_blocks_on_clock_sync_failure() -> None:
    ta = TimeAuthority(sync_status="failed", samples_requested=5, samples_kept=0)
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        time_authority=ta,
        entry_mode=Z_GAP_ENTRY_MODE_ENFORCE,
    )
    assert ev.reason_code == REASON_CLOCK_SYNC_FAILED


def test_binance_stale_feed() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(binance_freshness=FRESHNESS_STALE),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_FEED_STALE


def test_basis_exceeded_fresh_chainlink() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("5"), basis_status=BASIS_FRESH),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_BASIS_EXCEEDED


def test_chainlink_stale_basis_untrusted() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(
            chainlink_freshness=FRESHNESS_STALE,
            basis_status=BASIS_UNTRUSTED,
            basis_bps=Decimal("50"),
        ),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_CHAINLINK_STALE


def test_basis_codes_are_distinct() -> None:
    exceeded = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("10"), basis_status=BASIS_FRESH),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    stale = evaluate_z_gap_entry(
        signal=_signal(
            chainlink_freshness=FRESHNESS_STALE,
            basis_status=BASIS_UNTRUSTED,
            basis_bps=Decimal("10"),
        ),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert exceeded.reason_code == REASON_BASIS_EXCEEDED
    assert stale.reason_code == REASON_CHAINLINK_STALE
    assert exceeded.reason_code != stale.reason_code


def test_z_out_of_band() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(z=0.2),
        edge=_edge(_fair(z=0.2)),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_Z_OUT_OF_BAND


def test_tau_out_of_band() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(tau_s=30.0),
        edge=_edge(_fair(tau_s=30.0)),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_TAU_OUT_OF_BAND


def test_sigma_not_ready() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(ready=False, reject_reason="min_samples_not_met"),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_SIGMA_NOT_READY


def test_jump_guard() -> None:
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(jump_guard_tripped=True, ready=False),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_JUMP_GUARD


def test_fee_model_unknown() -> None:
    unknown = parse_fee_model_from_raw(None)
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=compute_edge(
            _fair(),
            ask_up=Decimal("0.61"),
            ask_down=Decimal("0.39"),
            fee_model=unknown,
        ),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=unknown,
    )
    assert ev.reason_code == REASON_FEE_MODEL_UNKNOWN


def test_book_stale() -> None:
    books = _books()
    stale_up = LegBookQuote(
        token_id="111",
        bid=books.up.bid,
        ask=books.up.ask,
        stale=True,
        book_age_ms=9000,
        spread=books.up.spread,
        quality_status=QUALITY_OK,
    )
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=_fair(),
        edge=_edge(_fair()),
        vol=_vol(),
        books=PairBookSnapshot(up=stale_up, down=books.down),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_BOOK_STALE


def test_edge_below_theta() -> None:
    fair = _fair(p_up=0.52, p_down=0.48, z=1.0)
    books = PairBookSnapshot(
        up=LegBookQuote("111", Decimal("0.50"), Decimal("0.51"), False, 100, Decimal("0.01"), QUALITY_OK),
        down=LegBookQuote("222", Decimal("0.48"), Decimal("0.49"), False, 100, Decimal("0.01"), QUALITY_OK),
    )
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.51"),
        ask_down=Decimal("0.49"),
        fee_model=FEE_MODEL,
        expected_slippage_up=Decimal("0.01"),
        expected_slippage_down=Decimal("0.01"),
    )
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=fair,
        edge=edge,
        vol=_vol(),
        books=books,
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.reason_code == REASON_EDGE_BELOW_THETA


def test_would_enter_no_oms() -> None:
    fair = _fair(p_up=0.80, p_down=0.20, z=1.5)
    ev = evaluate_z_gap_entry(
        signal=_signal(basis_bps=Decimal("1")),
        fair=fair,
        edge=compute_edge(
            fair,
            ask_up=Decimal("0.50"),
            ask_down=Decimal("0.50"),
            fee_model=FEE_MODEL,
            expected_slippage_up=Decimal("0.01"),
            expected_slippage_down=Decimal("0.01"),
        ),
        vol=_vol(),
        books=_books(),
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
    )
    assert ev.decision_status == DECISION_WOULD_ENTER
    assert ev.reason_code is None
    assert ev.selected_edge is not None
    assert ev.selected_edge >= ENTRY.theta_take
