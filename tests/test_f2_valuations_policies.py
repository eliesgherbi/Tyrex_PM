"""F2: entry/position valuations, leg selection, policies, precedence."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.domain.polymarket.fees import (
    FeeEstimateKind,
    PROVISIONAL_SAMPLE_FEE,
    phi_taker_fee_per_share,
)
from tyrex_pm.core.ids import CorrelationId, MarketId
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.z_gap.config import ZGapConfig, ZGapEntryConfig
from tyrex_pm.strategies.z_gap.policies import (
    NormalizedRiskFlags,
    ResolutionPreference,
    combine_precedence,
    evaluate_readiness,
    evaluate_realization,
    evaluate_thesis,
    evaluate_time_resolution,
    select_leg,
)
from tyrex_pm.strategies.z_gap.reasons import ZGapReason
from tyrex_pm.strategies.z_gap.snapshots import DecisionEpoch, ZGapModelSnapshot
from tyrex_pm.strategies.z_gap.state import ThesisConfirmState
from tyrex_pm.strategies.z_gap.valuations import (
    LegBookView,
    PositionView,
    ZGapLeg,
    value_entry_leg,
    value_position,
)
from tyrex_pm.domain.polymarket.ptb import PtbQuality, make_fixture_ptb
from tyrex_pm.indicators.reference_basis import compute_basis_bps

TS = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
MID = MarketId("m-val")


def _epoch(window: str = "w1") -> DecisionEpoch:
    return DecisionEpoch.new(
        market_id=MID,
        window_id=window,
        evaluated_at=TS,
        correlation_id=CorrelationId("c-val"),
    )


def _model(
    *,
    p_up: float = 0.70,
    p_down: float = 0.30,
    z: float = 0.5,
    tau_s: float = 120.0,
    ready: bool = True,
    epoch: DecisionEpoch | None = None,
) -> ZGapModelSnapshot:
    from datetime import timedelta

    ep = epoch or _epoch()
    ptb = make_fixture_ptb(
        market_id=MID,
        window_id=ep.window_id,
        event_start=TS,
        event_end=TS + timedelta(minutes=5),
        k="100",
        receive_ts=TS,
        quality=PtbQuality.CONFIRMED_CANONICAL,
    )
    return ZGapModelSnapshot(
        epoch=ep,
        S=Decimal("100"),
        K=Decimal("100"),
        sigma=0.01,
        sigma_units="per_sqrt_second",
        tau_s=tau_s,
        z=z,
        p_up=p_up,
        p_down=p_down,
        basis_bps=Decimal("0"),
        ptb=ptb,
        ready=ready,
    )


def test_fee_signs_estimated_not_confirmed() -> None:
    ask = Decimal("0.50")
    fee = phi_taker_fee_per_share(ask, PROVISIONAL_SAMPLE_FEE)
    assert fee == Decimal("0.0175")
    cfg = ZGapConfig()
    model = _model(p_up=0.70, p_down=0.30)
    up = value_entry_leg(
        model=model,
        leg=ZGapLeg.UP,
        book=LegBookView(ask=ask, ask_depth=Decimal("10"), bid=None, bid_depth=None),
        config=cfg,
    )
    assert up.ready
    assert up.fee_kind is FeeEstimateKind.ESTIMATED
    assert up.c_entry_unit == ask + fee + cfg.entry.expected_slippage_buy
    assert up.e_settlement == up.fair_probability - up.c_entry_unit
    assert up.e_repricing == up.e_settlement - up.f_exit_estimated


def test_entry_no_ask_not_ready() -> None:
    model = _model()
    v = value_entry_leg(
        model=model,
        leg=ZGapLeg.UP,
        book=LegBookView(ask=None, ask_depth=None, bid=None, bid_depth=None, ready=False),
        config=ZGapConfig(),
    )
    assert not v.ready
    assert ZGapReason.BOOK_NOT_READY.value in v.reason_codes


def test_leg_selection_up_down_and_less_probable() -> None:
    from dataclasses import replace

    cfg = replace(
        ZGapConfig(
            entry=ZGapEntryConfig(
                theta_take=Decimal("0.01"),
                expected_slippage_buy=Decimal("0"),
                exit_friction_reserve=Decimal("0"),
                reject_both_legs_edge=False,
                tie_epsilon=Decimal("0.0001"),
            )
        ),
        realization=replace(
            ZGapConfig().realization,
            expected_slippage_sell=Decimal("0"),
            slippage_included_in_executable_bid=True,
        ),
    )

    model = _model(p_up=0.70, p_down=0.30, z=1.0)
    # UP expensive, DOWN cheap → less-probable DOWN wins
    up = value_entry_leg(
        model=model,
        leg=ZGapLeg.UP,
        book=LegBookView(
            ask=Decimal("0.69"), ask_depth=Decimal("20"), bid=None, bid_depth=None
        ),
        config=cfg,
    )
    down = value_entry_leg(
        model=model,
        leg=ZGapLeg.DOWN,
        book=LegBookView(
            ask=Decimal("0.14"), ask_depth=Decimal("20"), bid=None, bid_depth=None
        ),
        config=cfg,
    )
    assert up.ready and down.ready
    assert down.e_repricing is not None and up.e_repricing is not None
    assert down.e_repricing > up.e_repricing
    sel = select_leg(up, down, config=cfg)
    assert sel.selected is ZGapLeg.DOWN
    assert sel.reason_code is ZGapReason.ENTRY_CANDIDATE

    # UP clearly better
    up2 = value_entry_leg(
        model=model,
        leg=ZGapLeg.UP,
        book=LegBookView(
            ask=Decimal("0.40"), ask_depth=Decimal("20"), bid=None, bid_depth=None
        ),
        config=cfg,
    )
    down2 = value_entry_leg(
        model=model,
        leg=ZGapLeg.DOWN,
        book=LegBookView(
            ask=Decimal("0.40"), ask_depth=Decimal("20"), bid=None, bid_depth=None
        ),
        config=cfg,
    )
    sel_up = select_leg(up2, down2, config=cfg)
    assert sel_up.selected is ZGapLeg.UP


def test_both_legs_edge_and_threshold() -> None:
    from dataclasses import replace

    cfg = ZGapConfig(
        entry=ZGapEntryConfig(
            theta_take=Decimal("0.50"),  # very high
            expected_slippage_buy=Decimal("0"),
            exit_friction_reserve=Decimal("0"),
            reject_both_legs_edge=True,
        )
    )
    cfg = replace(
        cfg,
        realization=replace(
            cfg.realization,
            expected_slippage_sell=Decimal("0"),
            slippage_included_in_executable_bid=True,
        ),
    )
    model = _model(p_up=0.9, p_down=0.1)
    up = value_entry_leg(
        model=model,
        leg=ZGapLeg.UP,
        book=LegBookView(
            ask=Decimal("0.50"), ask_depth=Decimal("5"), bid=None, bid_depth=None
        ),
        config=cfg,
    )
    down = value_entry_leg(
        model=model,
        leg=ZGapLeg.DOWN,
        book=LegBookView(
            ask=Decimal("0.05"), ask_depth=Decimal("5"), bid=None, bid_depth=None
        ),
        config=cfg,
    )
    sel = select_leg(up, down, config=cfg)
    assert sel.selected is None
    assert sel.reason_code in {ZGapReason.BELOW_THRESHOLD, ZGapReason.NO_ECONOMIC_EDGE}

    # Both legs edge reject
    cfg2 = replace(
        cfg,
        entry=replace(
            cfg.entry, theta_take=Decimal("0.01"), reject_both_legs_edge=True
        ),
    )
    model2 = _model(p_up=0.6, p_down=0.4)
    up_b = value_entry_leg(
        model=model2,
        leg=ZGapLeg.UP,
        book=LegBookView(
            ask=Decimal("0.30"), ask_depth=Decimal("5"), bid=None, bid_depth=None
        ),
        config=cfg2,
    )
    down_b = value_entry_leg(
        model=model2,
        leg=ZGapLeg.DOWN,
        book=LegBookView(
            ask=Decimal("0.20"), ask_depth=Decimal("5"), bid=None, bid_depth=None
        ),
        config=cfg2,
    )
    sel_b = select_leg(up_b, down_b, config=cfg2)
    assert sel_b.both_legs_edge or sel_b.reason_code is ZGapReason.BOTH_LEGS_EDGE


def test_position_valuation_sell_signs_and_no_double_slip() -> None:
    from dataclasses import replace

    cfg = replace(
        ZGapConfig(),
        realization=replace(
            ZGapConfig().realization,
            expected_slippage_sell=Decimal("0.01"),
            slippage_included_in_executable_bid=True,
            theta_rich=Decimal("0.02"),
        ),
    )
    ep = _epoch()
    model = _model(p_up=0.55, p_down=0.45, epoch=ep)
    pos = PositionView(
        epoch=ep,
        held_leg=ZGapLeg.UP,
        confirmed_quantity=Decimal("10"),
        entry_cost_total=Decimal("5.0"),
    )
    bid = Decimal("0.60")
    fee = phi_taker_fee_per_share(bid, cfg.friction.fee_curve)
    val = value_position(
        model=model,
        position=pos,
        book=LegBookView(
            ask=None, ask_depth=None, bid=bid, bid_depth=Decimal("10"), ready=True
        ),
        config=cfg,
    )
    assert val.ready
    assert val.slippage_sell_applied == Decimal("0")  # included in executable
    assert val.v_sell_unit == bid - fee
    assert val.market_richness == val.v_sell_unit - val.p_held
    assert val.remaining_hold_edge == val.p_held - val.v_sell_unit
    assert val.pnl_liquidation == val.v_sell - pos.entry_cost_total
    assert val.fee_kind is FeeEstimateKind.ESTIMATED

    # Epoch mismatch
    other = _epoch("w2")
    bad = value_position(
        model=model,
        position=PositionView(
            epoch=other,
            held_leg=ZGapLeg.UP,
            confirmed_quantity=Decimal("1"),
            entry_cost_total=Decimal("1"),
        ),
        book=LegBookView(
            ask=None, ask_depth=None, bid=bid, bid_depth=Decimal("1"), ready=True
        ),
        config=cfg,
    )
    assert not bad.ready
    assert ZGapReason.EPOCH_MISMATCH.value in bad.reason_codes


def test_thesis_confirmation_and_reset() -> None:
    cfg = ZGapConfig()
    # Start adverse
    r1 = evaluate_thesis(
        p_held=0.30,
        model_valid=True,
        model_fresh=True,
        now_mono_ns=0,
        prior=ThesisConfirmState(),
        config=cfg,
    )
    assert r1.confirming and r1.reason_code is ZGapReason.THESIS_CONFIRMING
    # Confirm after 1s
    r2 = evaluate_thesis(
        p_held=0.30,
        model_valid=True,
        model_fresh=True,
        now_mono_ns=1_000_000_000,
        prior=r1.state,
        config=cfg,
    )
    assert r2.invalidated and r2.reason_code is ZGapReason.THESIS_INVALID
    # Recovery
    r3 = evaluate_thesis(
        p_held=0.50,
        model_valid=True,
        model_fresh=True,
        now_mono_ns=2_000_000_000,
        prior=r2.state,
        config=cfg,
    )
    assert r3.valid and r3.reason_code is ZGapReason.THESIS_VALID
    # Stale reset
    r4 = evaluate_thesis(
        p_held=0.50,
        model_valid=False,
        model_fresh=False,
        now_mono_ns=3_000_000_000,
        prior=r3.state,
        config=cfg,
    )
    assert r4.reason_code is ZGapReason.THESIS_RESET_STALE


def test_realization_and_sell_beats_resolution() -> None:
    from dataclasses import replace

    cfg = replace(
        ZGapConfig(),
        realization=replace(ZGapConfig().realization, theta_rich=Decimal("0.02")),
    )
    ep = _epoch()
    model = _model(p_up=0.50, epoch=ep)
    pos = PositionView(
        epoch=ep,
        held_leg=ZGapLeg.UP,
        confirmed_quantity=Decimal("10"),
        entry_cost_total=Decimal("4"),
    )
    # Rich bid vs p=0.50
    val = value_position(
        model=model,
        position=pos,
        book=LegBookView(
            ask=None,
            ask_depth=None,
            bid=Decimal("0.60"),
            bid_depth=Decimal("10"),
            ready=True,
        ),
        config=cfg,
    )
    rich = evaluate_realization(val, config=cfg)
    assert rich.exit_rich and rich.reason_code is ZGapReason.MARKET_RICH_EXIT

    # Sell beats resolution hold
    tr = evaluate_time_resolution(
        tau_s=100.0,
        resolution_capability=True,
        v_sell=Decimal("6.0"),
        v_resolve_adj=Decimal("5.0"),
        config=cfg,
    )
    assert tr.preference is ResolutionPreference.SELL
    assert tr.reason_code is ZGapReason.SELL_BEATS_RESOLUTION

    hold = evaluate_time_resolution(
        tau_s=100.0,
        resolution_capability=True,
        v_sell=Decimal("4.0"),
        v_resolve_adj=Decimal("5.0"),
        config=cfg,
    )
    assert hold.preference is ResolutionPreference.HOLD_RESOLUTION

    # With capability on, near-deadline still re-compares (non-sticky); resolve can win.
    deadline = evaluate_time_resolution(
        tau_s=10.0,
        resolution_capability=True,
        v_sell=Decimal("4.0"),
        v_resolve_adj=Decimal("5.0"),
        config=cfg,
    )
    assert deadline.preference is ResolutionPreference.HOLD_RESOLUTION
    assert deadline.reason_code is ZGapReason.RESOLUTION_PREFERENCE

    # TIME_SELL only when resolution capability is unavailable.
    time_sell = evaluate_time_resolution(
        tau_s=10.0,
        resolution_capability=False,
        v_sell=Decimal("4.0"),
        v_resolve_adj=Decimal("5.0"),
        config=cfg,
    )
    assert time_sell.preference is ResolutionPreference.SELL
    assert time_sell.reason_code is ZGapReason.TIME_SELL


def test_precedence_unknown_emergency_thesis_rich() -> None:
    d1 = combine_precedence(
        flags=NormalizedRiskFlags(unknown_inventory=True),
        model_valid=True,
        thesis=None,
        realization=None,
        time_res=None,
        flat=False,
    )
    assert d1.action is StrategyAction.BLOCKED

    d2 = combine_precedence(
        flags=NormalizedRiskFlags(emergency=True),
        model_valid=True,
        thesis=None,
        realization=None,
        time_res=None,
        flat=False,
    )
    assert d2.action is StrategyAction.FLATTEN

    from tyrex_pm.strategies.z_gap.policies import RealizationResult, ThesisPolicyResult
    from tyrex_pm.strategies.z_gap.state import ThesisConfirmPhase

    thesis_bad = ThesisPolicyResult(
        valid=False,
        confirming=False,
        invalidated=True,
        state=ThesisConfirmState(phase=ThesisConfirmPhase.INVALIDATED),
        reason_code=ZGapReason.THESIS_INVALID,
    )
    d3 = combine_precedence(
        flags=NormalizedRiskFlags(),
        model_valid=True,
        thesis=thesis_bad,
        realization=RealizationResult(exit_rich=True, reason_code=ZGapReason.MARKET_RICH_EXIT),
        time_res=None,
        flat=False,
    )
    # Thesis (L4) before rich exit (L5)
    assert d3.action is StrategyAction.EXIT
    assert d3.reason_code is ZGapReason.THESIS_INVALID


def test_readiness_gates() -> None:
    from datetime import timedelta

    cfg = ZGapConfig()
    model = _model(tau_s=120, z=1.0)
    ptb = make_fixture_ptb(
        market_id=MID,
        window_id="w1",
        event_start=TS,
        event_end=TS + timedelta(minutes=5),
        k="100",
        receive_ts=TS,
    )
    basis = compute_basis_bps(trading_ref="100", settlement_ref="100")
    ok = evaluate_readiness(
        model=model, ptb=ptb, basis=basis, time_ready=True, config=cfg
    )
    assert ok.ready
    bad_time = evaluate_readiness(
        model=model, ptb=ptb, basis=basis, time_ready=False, config=cfg
    )
    assert bad_time.action is StrategyAction.WAIT
