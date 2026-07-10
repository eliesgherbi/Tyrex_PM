"""Tests for Z-Gap calibration sample capture (A0.5)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot, QUALITY_OK
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import compute_edge
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import _parse_z_gap_entry
from tyrex_pm.state.signal_state_store import (
    BASIS_FRESH,
    FRESHNESS_FRESH,
    FRESHNESS_LATE,
    FRESHNESS_OBSERVED,
    FRESHNESS_PENDING,
    SignalSnapshot,
)
from tyrex_pm.strategies.z_gap.calibration_samples import (
    DEFAULT_CALIBRATION_SAMPLES_PATH,
    DEFAULT_INFRA_DEBUG_SAMPLES_PATH,
    append_calibration_sample,
    build_calibration_sample_row,
)
from tyrex_pm.strategies.z_gap.entry_eval import DECISION_SKIP, REASON_TAU_OUT_OF_BAND, evaluate_z_gap_entry

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
ENTRY = _parse_z_gap_entry({})
FEE = parse_fee_model_from_raw({"fd": {"r": 0.07, "e": 1, "to": True}})


def _fixtures():
    signal = SignalSnapshot(
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
        ptb_lag_ms=840.0,
        basis_bps=Decimal("1.1"),
        basis_status=BASIS_FRESH,
        feed_reject_reason=None,
        snapshot_ts=TS,
    )
    fair = FairValueSnapshot(
        S=Decimal("100100"),
        K=Decimal("100000"),
        tau_s=30.0,
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        z=0.33,
        p_up=0.63,
        p_down=0.37,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )
    edge = compute_edge(
        fair,
        ask_up=Decimal("0.61"),
        ask_down=Decimal("0.39"),
        fee_model=FEE,
    )
    vol = VolatilitySnapshot(
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=25,
        effective_samples_s=25.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )
    books = PairBookSnapshot(
        up=LegBookQuote("111", Decimal("0.60"), Decimal("0.61"), False, 100, Decimal("0.01"), QUALITY_OK),
        down=LegBookQuote("222", Decimal("0.38"), Decimal("0.39"), False, 100, Decimal("0.01"), QUALITY_OK),
    )
    evaln = evaluate_z_gap_entry(
        signal=signal,
        fair=fair,
        edge=edge,
        vol=vol,
        books=books,
        entry_cfg=ENTRY,
        fee_model=FEE,
    )
    return signal, fair, edge, vol, evaln


def test_row_written_for_observe_window(tmp_path: Path) -> None:
    signal, fair, edge, vol, evaln = _fixtures()
    row = build_calibration_sample_row(
        market_id="btc_5m_test",
        condition_id="0xabc",
        entry_mode="observe_only",
        evaln=evaln,
        fair=fair,
        edge=edge,
        vol=vol,
        signal=signal,
        fee_model=FEE,
        resolved_outcome=None,
    )
    path = append_calibration_sample(row, path=tmp_path / "samples.jsonl")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["market_id"] == "btc_5m_test"
    assert data["entry_mode"] == "observe_only"
    assert data["resolved_outcome"] is None


def test_row_includes_p_z_edge_fields() -> None:
    signal, fair, edge, vol, evaln = _fixtures()
    row = build_calibration_sample_row(
        market_id="btc_5m_test",
        condition_id="0xabc",
        entry_mode="observe_only",
        evaln=evaln,
        fair=fair,
        edge=edge,
        vol=vol,
        signal=signal,
        fee_model=FEE,
    )
    d = row.to_dict()
    assert d["p_up_at_decision"] == "0.63"
    assert d["z_at_decision"] == "0.33"
    assert d["edge_up"] is not None
    assert d["sigma"] == "0.00015"
    assert d["sigma_units"] == "per_sqrt_second"
    assert evaln.decision_status == DECISION_SKIP
    assert d["skip_reason_top"] == REASON_TAU_OUT_OF_BAND


def test_late_ptb_goes_to_infra_debug_path(tmp_path: Path) -> None:
    signal, fair, edge, vol, evaln = _fixtures()
    signal = replace(signal, ptb_status=FRESHNESS_LATE)
    row = build_calibration_sample_row(
        market_id="btc_5m_test",
        condition_id="0xabc",
        entry_mode="observe_only",
        evaln=evaln,
        fair=fair,
        edge=edge,
        vol=vol,
        signal=signal,
        fee_model=FEE,
    )
    assert row.calibration_usable is False
    path = append_calibration_sample(
        row,
        path=tmp_path / "calibration_samples.jsonl",
        infra_debug_path=tmp_path / "observe_infra_debug_samples.jsonl",
    )
    assert path.name == "observe_infra_debug_samples.jsonl"
    assert not (tmp_path / "calibration_samples.jsonl").exists()


def test_default_path_constant() -> None:
    assert DEFAULT_CALIBRATION_SAMPLES_PATH.as_posix().endswith(
        "var/reporting/z_gap/calibration_samples.jsonl"
    )
    assert DEFAULT_INFRA_DEBUG_SAMPLES_PATH.as_posix().endswith(
        "var/reporting/z_gap/observe_infra_debug_samples.jsonl"
    )
