"""Tests for Z-Gap entry plan construction (A0.6)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.market_data.book_read import LegBookQuote, PairBookSnapshot, QUALITY_OK
from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY
from tyrex_pm.quant.edge import EDGE_STATUS_READY, EdgeSnapshot, LEG_DOWN, LEG_UP, compute_edge
from tyrex_pm.quant.fees import parse_fee_model_from_raw
from tyrex_pm.runtime.config import Z_GAP_ENTRY_MODE_OBSERVE_ONLY, _parse_z_gap_entry
from tyrex_pm.runtime.config import ZGapSizingConfig
from tyrex_pm.strategies.z_gap.entry_eval import (
    DECISION_SKIP,
    DECISION_WOULD_ENTER,
    ZGapEntryEvaluation,
    evaluate_z_gap_entry,
)
from tyrex_pm.strategies.z_gap.entry_plan import (
    PLAN_STATUS_BLOCKED,
    PLAN_STATUS_READY,
    REASON_EVAL_SKIP,
    REASON_FEE_MODEL_UNKNOWN,
    REASON_MODEL_CAP,
    build_z_gap_entry_plan,
)

TS = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
ENTRY = _parse_z_gap_entry({"theta_fill_floor": "0.02", "theta_take": "0.01"})
FD_RAW = {"fd": {"r": 0.07, "e": 1, "to": True}}
FEE_MODEL = parse_fee_model_from_raw(FD_RAW)
SIZING = ZGapSizingConfig(mode="fixed_usd", max_usd=Decimal("5"), min_shares=Decimal("5"))
TICK = Decimal("0.01")
YES = "111"
NO = "222"


def _books(ask_up: str = "0.61", ask_down: str = "0.39") -> PairBookSnapshot:
    return PairBookSnapshot(
        up=LegBookQuote(YES, Decimal("0.60"), Decimal(ask_up), False, 100, TICK, QUALITY_OK),
        down=LegBookQuote(NO, Decimal("0.38"), Decimal(ask_down), False, 100, TICK, QUALITY_OK),
    )


def _fair_up() -> FairValueSnapshot:
    return FairValueSnapshot(
        S=Decimal("100100"),
        K=Decimal("100000"),
        tau_s=120.0,
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        z=1.2,
        p_up=Decimal("0.65"),
        p_down=Decimal("0.35"),
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )


def _edge(fair: FairValueSnapshot, books: PairBookSnapshot) -> EdgeSnapshot:
    return compute_edge(
        fair,
        ask_up=books.up.ask,
        ask_down=books.down.ask,
        fee_model=FEE_MODEL,
        expected_slippage_up=TICK,
        expected_slippage_down=TICK,
    )


def _would_enter_eval(*, fair: FairValueSnapshot | None = None, books: PairBookSnapshot | None = None) -> ZGapEntryEvaluation:
    from tyrex_pm.quant.volatility import VolatilitySnapshot
    from tyrex_pm.state.signal_state_store import BASIS_FRESH, FRESHNESS_FRESH, FRESHNESS_OBSERVED, SignalSnapshot

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
        ptb_lag_ms=0.0,
        basis_bps=Decimal("1"),
        basis_status=BASIS_FRESH,
        feed_reject_reason=None,
        snapshot_ts=TS,
    )
    fair = fair or _fair_up()
    books = books or _books()
    vol = VolatilitySnapshot(
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        ready=True,
        sample_count=30,
        effective_samples_s=25.0,
        last_update_ts=TS,
        jump_guard_tripped=False,
        reject_reason=None,
    )
    edge = _edge(fair, books)
    return evaluate_z_gap_entry(
        signal=signal,
        fair=fair,
        edge=edge,
        vol=vol,
        books=books,
        entry_cfg=ENTRY,
        fee_model=FEE_MODEL,
        entry_mode=Z_GAP_ENTRY_MODE_OBSERVE_ONLY,
    )


def test_would_enter_up_creates_up_plan() -> None:
    evaln = _would_enter_eval()
    assert evaln.decision_status == DECISION_WOULD_ENTER
    assert evaln.selected_leg == LEG_UP
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), _books()),
        books=_books(),
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="btc_5m_test",
        condition_id="0xabc",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.selected_leg == LEG_UP
    assert plan.token_id == YES
    assert plan.plan_status == PLAN_STATUS_READY


def test_would_enter_down_creates_down_plan() -> None:
    fair = FairValueSnapshot(
        S=Decimal("100100"),
        K=Decimal("100000"),
        tau_s=120.0,
        sigma=0.00015,
        sigma_units="per_sqrt_second",
        z=1.2,
        p_up=Decimal("0.35"),
        p_down=Decimal("0.65"),
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=TS,
    )
    books = _books(ask_up="0.55", ask_down="0.40")
    evaln = _would_enter_eval(fair=fair, books=books)
    assert evaln.selected_leg == LEG_DOWN
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=fair,
        edge=_edge(fair, books),
        books=books,
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="btc_5m_test",
        condition_id="0xabc",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.selected_leg == LEG_DOWN
    assert plan.token_id == NO


def test_skip_evaluation_creates_blocked_plan() -> None:
    evaln = ZGapEntryEvaluation(
        decision_status=DECISION_SKIP,
        selected_leg=LEG_UP,
        selected_edge=Decimal("0.01"),
        reason_code=REASON_EVAL_SKIP,
        gate_results={},
        fair_value_snapshot=None,
        edge_snapshot=None,
        signal_snapshot=None,
        book_snapshot_summary=None,
        decision_ts=TS,
    )
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), _books()),
        books=_books(),
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="m",
        condition_id="c",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.plan_status == PLAN_STATUS_BLOCKED
    assert plan.shares is None


def test_missing_fee_model_blocks() -> None:
    evaln = ZGapEntryEvaluation(
        decision_status=DECISION_WOULD_ENTER,
        selected_leg=LEG_UP,
        selected_edge=Decimal("0.10"),
        reason_code=None,
        gate_results={},
        fair_value_snapshot=None,
        edge_snapshot=None,
        signal_snapshot=None,
        book_snapshot_summary=None,
        decision_ts=TS,
    )
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), _books()),
        books=_books(),
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=None,
        market_id="m",
        condition_id="c",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.reject_reason == REASON_FEE_MODEL_UNKNOWN


def test_model_cap_below_ask_uses_model_cap() -> None:
    evaln = _would_enter_eval()
    books = _books(ask_up="0.70")
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), books),
        books=books,
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="m",
        condition_id="c",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.plan_status == PLAN_STATUS_READY
    assert plan.final_limit_price is not None
    assert plan.ask_seen == Decimal("0.70")
    assert plan.final_limit_price <= plan.ask_seen
    assert plan.final_limit_price <= plan.max_fill_price


def test_model_cap_infeasible_blocks() -> None:
    evaln = ZGapEntryEvaluation(
        decision_status=DECISION_WOULD_ENTER,
        selected_leg=LEG_UP,
        selected_edge=Decimal("0.10"),
        reason_code=None,
        gate_results={},
        fair_value_snapshot=None,
        edge_snapshot=None,
        signal_snapshot=None,
        book_snapshot_summary=None,
        decision_ts=TS,
    )
    tight_entry = _parse_z_gap_entry({"theta_fill_floor": "0.70", "theta_take": "0.01"})
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), _books()),
        books=_books(),
        entry_cfg=tight_entry,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="m",
        condition_id="c",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.plan_status == PLAN_STATUS_BLOCKED
    assert plan.reject_reason == REASON_MODEL_CAP


def test_predicted_edge_at_limit_meets_floor() -> None:
    evaln = _would_enter_eval()
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), _books()),
        books=_books(),
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="m",
        condition_id="c",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    assert plan.predicted_edge_at_limit is not None
    assert plan.theta_fill_floor is not None
    assert plan.predicted_edge_at_limit >= plan.theta_fill_floor


def test_fact_payload_includes_required_fields() -> None:
    evaln = _would_enter_eval()
    plan = build_z_gap_entry_plan(
        evaln=evaln,
        fair=_fair_up(),
        edge=_edge(_fair_up(), _books()),
        books=_books(),
        entry_cfg=ENTRY,
        sizing_cfg=SIZING,
        fee_model=FEE_MODEL,
        market_id="m1",
        condition_id="c1",
        yes_token_id=YES,
        no_token_id=NO,
        entry_mode="enforce",
        tick_size=TICK,
    )
    payload = plan.to_fact_payload()
    required = {
        "market_id",
        "condition_id",
        "selected_leg",
        "token_id",
        "p_L",
        "ask_seen",
        "theta_fill_floor",
        "max_fill_price",
        "final_limit_price",
        "phi_at_limit",
        "expected_slippage",
        "predicted_edge_at_limit",
        "shares",
        "notional_usd",
        "order_style",
        "time_in_force",
        "plan_status",
        "reject_reason",
        "entry_mode",
    }
    assert required.issubset(payload.keys())
    assert payload["order_style"] == "FAK"
    assert payload["time_in_force"] == "FAK"
