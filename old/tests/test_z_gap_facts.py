"""Tests for Z-Gap fact emission and dedup (A0.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import RunId
from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot, QUALITY_OK
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import EDGE_STATUS_READY, EdgeSnapshot
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.quant.volatility import VolatilitySnapshot
from tyrex_pm.runtime.config import _parse_z_gap_entry
from tyrex_pm.state.signal_state_store import BASIS_FRESH, BASIS_UNTRUSTED, FRESHNESS_FRESH, FRESHNESS_OBSERVED, SignalSnapshot
from tyrex_pm.strategies.z_gap import facts as zg_facts
from tyrex_pm.strategies.z_gap.entry_eval import (
    REASON_BASIS_EXCEEDED,
    REASON_CHAINLINK_STALE,
    evaluate_z_gap_entry,
)

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
ENTRY = _parse_z_gap_entry({})
FEE = parse_fee_model_from_raw({"fd": {"r": 0.07, "e": 1, "to": True}})


class _Sink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write(self, obj: dict) -> None:
        self.rows.append(obj)


def _signal(basis_bps: str, basis_status: str, chainlink_freshness: str = FRESHNESS_FRESH) -> SignalSnapshot:
    return SignalSnapshot(
        binance_price=Decimal("100100"),
        binance_source_ts=TS,
        binance_recv_ts=TS,
        binance_age_ms=100.0,
        binance_freshness=FRESHNESS_FRESH,
        chainlink_price=Decimal("100000"),
        chainlink_source_ts=TS,
        chainlink_recv_ts=TS,
        chainlink_age_ms=100.0,
        chainlink_freshness=chainlink_freshness,
        price_to_beat=Decimal("100000"),
        ptb_status=FRESHNESS_OBSERVED,
        ptb_observed_ts=TS,
        ptb_lag_ms=500.0,
        basis_bps=Decimal(basis_bps),
        basis_status=basis_status,
        feed_reject_reason=None,
        snapshot_ts=TS,
    )


def _evaln_for_signal(signal: SignalSnapshot):
    fair = FairValueSnapshot(
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
    edge = EdgeSnapshot(
        edge_up=Decimal("0.01"),
        edge_down=Decimal("-0.01"),
        selected_leg="UP",
        selected_edge=Decimal("0.01"),
        ask_up=Decimal("0.61"),
        ask_down=Decimal("0.39"),
        fee_up=Decimal("0.017"),
        fee_down=Decimal("0.017"),
        expected_slippage_up=Decimal("0.01"),
        expected_slippage_down=Decimal("0.01"),
        fee_model_id="polymarket_dynamic_fd_v1",
        edge_status=EDGE_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )
    books = PairBookSnapshot(
        up=LegBookQuote("111", Decimal("0.60"), Decimal("0.61"), False, 100, Decimal("0.01"), QUALITY_OK),
        down=LegBookQuote("222", Decimal("0.38"), Decimal("0.39"), False, 100, Decimal("0.01"), QUALITY_OK),
    )
    return evaluate_z_gap_entry(
        signal=signal,
        fair=fair,
        edge=edge,
        vol=VolatilitySnapshot(
            sigma=0.00015,
            sigma_units="per_sqrt_second",
            ready=True,
            sample_count=25,
            effective_samples_s=25.0,
            last_update_ts=TS,
            jump_guard_tripped=False,
            reject_reason=None,
        ),
        books=books,
        entry_cfg=ENTRY,
        fee_model=FEE,
    )


def test_required_fact_types_emitted() -> None:
    sink = _Sink()
    state = zg_facts.ZGapObserveRuntimeState()
    run_id = RunId("facts_test")
    signal = _signal("1", BASIS_FRESH)
    fair = FairValueSnapshot(
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
    edge = EdgeSnapshot(
        edge_up=Decimal("0.04"),
        edge_down=Decimal("-0.02"),
        selected_leg="UP",
        selected_edge=Decimal("0.04"),
        ask_up=Decimal("0.61"),
        ask_down=Decimal("0.39"),
        fee_up=Decimal("0.017"),
        fee_down=Decimal("0.017"),
        expected_slippage_up=Decimal("0.01"),
        expected_slippage_down=Decimal("0.01"),
        fee_model_id="polymarket_dynamic_fd_v1",
        edge_status=EDGE_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )

    zg_facts.emit_signal_feed_health(sink, run_id, state, signal)
    zg_facts.emit_basis_computed(sink, run_id, state, signal)
    zg_facts.emit_price_to_beat_observed(
        sink, run_id, state, market_id="m1", signal=signal, event_start_ts=1.0, event_end_ts=2.0
    )
    zg_facts.emit_model_state_snapshot(sink, run_id, state, fair, vol)
    zg_facts.emit_fee_model_resolved(sink, run_id, state, FEE, price=Decimal("0.61"))
    zg_facts.emit_edge_evaluated(sink, run_id, state, fair, edge)
    zg_facts.emit_entry_decision(sink, run_id, state, _evaln_for_signal(signal))

    types = {r["fact_type"] for r in sink.rows}
    assert "signal_feed_health" in types
    assert "basis_computed" in types
    assert "price_to_beat_observed" in types
    assert "model_state_snapshot" in types
    assert "fee_model_resolved" in types
    assert "edge_evaluated" in types
    assert "z_gap_entry_skip" in types or "z_gap_entry_eval" in types


def test_basis_skip_codes_distinct_in_entry_eval() -> None:
    exceeded = _evaln_for_signal(_signal("10", BASIS_FRESH))
    stale = _evaln_for_signal(_signal("10", BASIS_UNTRUSTED, chainlink_freshness="stale"))
    assert exceeded.reason_code == REASON_BASIS_EXCEEDED
    assert stale.reason_code == REASON_CHAINLINK_STALE


def test_basis_facts_deduped_on_repeat() -> None:
    sink = _Sink()
    state = zg_facts.ZGapObserveRuntimeState()
    run_id = RunId("dedup")
    signal = _signal("1", BASIS_FRESH)
    zg_facts.emit_basis_computed(sink, run_id, state, signal)
    zg_facts.emit_basis_computed(sink, run_id, state, signal)
    basis_facts = [r for r in sink.rows if r["fact_type"] == "basis_computed"]
    assert len(basis_facts) == 1
