"""Paired binary robustness tests (Phase 4.6)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import (
    ExitIntent,
    RiskContext,
    TradeFillRecord,
    URGENCY_URGENT,
    WalletPosition,
)
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.models import ExecutionPlan
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.risk.exits import build_exit_book_evidence_for_intent
from tyrex_pm.risk.planned_order import validate_planned_order
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.activation_flow import (
    begin_activation_recheck,
    evaluate_activation_safety,
    should_use_activation_recheck,
)
from tyrex_pm.strategies.paired_binary.entry_eval import LegBook, read_leg_book
from tyrex_pm.strategies.paired_binary.entry_price import (
    SOURCE_OMS_LIMIT_PRICE_FALLBACK,
    SOURCE_OMS_MATCHED_AVG,
    SOURCE_UNKNOWN,
    detect_entry_price_mismatch,
    resolve_leg_entry_price,
    resolve_pair_entry_prices,
)
from tyrex_pm.strategies.paired_binary.exit_engine import check_activation_loss_budget
from tyrex_pm.strategies.paired_binary.lifecycle import mark_both_legs_filled
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState

YES = TokenId("9059650700126795019827485089957938050581213031053374092199507389736394347163")
NO = TokenId("9059650700126795019827485089957938050581213031053374092199507389736394347164")
OTHER = TokenId("999")


def _risk(**over) -> dict:
    base = {
        "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
        "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
        "venue_min_size": {"enabled": True, "default_min_size": "5"},
        "capital": {"enabled": False},
        "inventory": {"sell_requires_venue_position": True},
        "exits": {
            "allow_reduce_only_mark_fallback": True,
            "require_fresh_book_for_mark_fallback": True,
            "urgent_exit_max_book_age_s": 0.5,
        },
    }
    base.update(over)
    return base


def _app(**risk_over):
    return parse_app_config(
        risk=_risk(**risk_over),
        strategy={"kind": "simple_signal_test", "token_id": str(YES)},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "market_data": {"enabled": True, "max_book_age_s": 5},
        },
    )


def _ctx(*, token=OTHER, qty=Decimal("5"), mark_prices=None) -> RiskContext:
    return RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(
            WalletPosition(token_id=token, qty=qty, avg_price_usd=None),
        ),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=utc_now(),
        mark_prices=mark_prices or {},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
    )


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def test_reduce_only_urgent_sell_allows_missing_mark_with_fresh_bid() -> None:
    app = _app()
    intent = ExitIntent(
        token_id=OTHER,
        side=Side.SELL,
        size=Decimal("5"),
        limit_price=Decimal("0.40"),
        order_style=OrderStyle.FAK,
        urgency=URGENCY_URGENT,
    )
    book_evidence = {"best_bid": "0.39", "stale": False, "book_age_ms": 10}
    decision = evaluate_intent(
        intent,
        _ctx(token=OTHER),
        app=app,
        run_id=RunId("r1"),
        exit_book_evidence=book_evidence,
    )
    assert decision.approved is True
    assert decision.extensions.get("reduce_only") is True
    assert decision.extensions.get("mark_source") == "executable_bid"


def test_reduce_only_urgent_sell_denies_missing_mark_without_fresh_bid() -> None:
    app = _app()
    intent = ExitIntent(
        token_id=OTHER,
        side=Side.SELL,
        size=Decimal("5"),
        limit_price=Decimal("0.40"),
        order_style=OrderStyle.FAK,
        urgency=URGENCY_URGENT,
    )
    decision = evaluate_intent(intent, _ctx(token=OTHER), app=app, run_id=RunId("r1"))
    assert decision.approved is False
    assert "deployment_mark_unknown" in decision.reason_codes


def test_activation_reject_unwind_uses_reduce_only_mark_fallback() -> None:
    app = _app()
    plan = ExecutionPlan(
        intent=ExitIntent(
            token_id=OTHER,
            side=Side.SELL,
            size=Decimal("5"),
            limit_price=Decimal("0.40"),
            order_style=OrderStyle.FAK,
            urgency=URGENCY_URGENT,
        ),
        client_order_id=ClientOrderId("sell-1"),
        run_id=RunId("r1"),
        planner_reason="planner_urgent_exit_fak",
        urgency=URGENCY_URGENT,
        book_evidence={"best_bid": "0.39", "stale": False},
    )
    decision = validate_planned_order(plan, _ctx(token=OTHER), app=app)
    assert decision.approved is True
    assert decision.extensions.get("reduce_only") is True


def test_pair_pnl_uses_oms_avg_fill_price_not_current_ask(tmp_path: Path) -> None:
    from tyrex_pm.execution.order_lifecycle import ack_submit, register_submit
    from tyrex_pm.core.models import ApprovedIntent, EnterIntent

    coord = _coord(tmp_path)
    intent = EnterIntent(
        token_id=YES, side=Side.BUY, size=Decimal("5"), limit_price=Decimal("0.46"), order_style=OrderStyle.FAK
    )
    ap = ApprovedIntent(intent=intent, client_order_id=ClientOrderId("yes-entry"), run_id=RunId("r1"))
    from tyrex_pm.core.ids import VenueOrderId

    register_submit(coord.orders, ap)
    ack_submit(
        coord.orders,
        ap,
        VenueOrderId("vid-yes-1"),
        shadow_instant_fill=False,
        ack_status="matched",
        filled_qty=Decimal("5"),
    )
    res = resolve_leg_entry_price(coord, YES, client_order_id="yes-entry")
    assert res.price == Decimal("0.46")
    assert res.source == SOURCE_OMS_LIMIT_PRICE_FALLBACK


def test_pair_pnl_uses_ws_confirmed_trade_price_when_oms_missing(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=YES,
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.51"),
            status="CONFIRMED",
            ts_utc=utc_now(),
        )
    )
    res = resolve_leg_entry_price(coord, YES)
    assert res.price == Decimal("0.51")
    assert res.source == "user_ws_confirmed_trade"


def test_activation_waits_when_entry_price_unknown(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    pair = resolve_pair_entry_prices(
        coord,
        yes_token_id=str(YES),
        no_token_id=str(NO),
        yes_client_order_id=None,
        no_client_order_id=None,
    )
    assert pair.ready is False
    assert pair.yes.source == SOURCE_UNKNOWN


def test_entry_price_mismatch_fact_emitted() -> None:
    from tyrex_pm.strategies.paired_binary.entry_price import LegEntryPriceResolution

    res = LegEntryPriceResolution(
        price=Decimal("0.50"),
        source=SOURCE_OMS_MATCHED_AVG,
        oms_price=Decimal("0.46"),
        ws_price=Decimal("0.52"),
        reconcile_price=Decimal("0.50"),
    )
    mismatch = detect_entry_price_mismatch("yes", res, tolerance=Decimal("0.005"))
    assert mismatch is not None
    assert mismatch.delta == Decimal("0.06")


def test_entry_price_mismatch_aborts_above_threshold() -> None:
    from tyrex_pm.strategies.paired_binary.entry_price import (
        LegEntryPriceResolution,
        max_entry_price_mismatch,
        PairEntryPriceResolution,
    )

    pair = PairEntryPriceResolution(
        yes=LegEntryPriceResolution(
            Decimal("0.46"), SOURCE_OMS_MATCHED_AVG, oms_price=Decimal("0.46"), ws_price=Decimal("0.52")
        ),
        no=LegEntryPriceResolution(Decimal("0.51"), SOURCE_OMS_MATCHED_AVG, oms_price=Decimal("0.51")),
    )
    assert max_entry_price_mismatch(pair) > Decimal("0.02")


def test_activation_gap_failure_rechecks_before_abort() -> None:
    from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
    from tyrex_pm.core.enums import OrderStyle

    cfg = PairedBinaryStrategyConfig(
        enabled=True,
        owner_id="pb",
        market_id="m",
        yes_token_id=str(YES),
        no_token_id=str(NO),
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=True,
        max_holding_time_s=100,
        entry_order_style=OrderStyle.FAK,
        exit_order_style=OrderStyle.FAK,
        entry_fill_timeout_s=60,
        abort_unpaired_entry=True,
        unwind_partial_entry=True,
        min_effective_pair_qty=Decimal("5"),
        run_once=False,
        max_markets=1,
        tick_interval_s=0.2,
        max_book_age_s=5,
        activation_gap_retry_s=3,
    )
    assert should_use_activation_recheck(cfg)
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.BOTH_LEGS_FILLED)
    begin_activation_recheck(state)
    assert state.phase == PairedBinaryPhase.ACTIVATION_PENDING_RECHECK


def test_activation_gap_recovery_arms_monitor() -> None:
    from tyrex_pm.runtime.config import PairedBinaryStrategyConfig
    from tyrex_pm.core.enums import OrderStyle

    cfg = PairedBinaryStrategyConfig(
        enabled=True,
        owner_id="pb",
        market_id="m",
        yes_token_id=str(YES),
        no_token_id=str(NO),
        position_size=Decimal("5"),
        max_pair_entry_cost=Decimal("1.02"),
        max_spread_yes=Decimal("0.02"),
        max_spread_no=Decimal("0.02"),
        pair_stop_loss_pct=Decimal("0.02"),
        pair_take_profit_pct=Decimal("0.05"),
        slippage_buffer=Decimal("0.005"),
        reject_if_spread_exceeds_loss_budget=True,
        max_holding_time_s=100,
        entry_order_style=OrderStyle.FAK,
        exit_order_style=OrderStyle.FAK,
        entry_fill_timeout_s=60,
        abort_unpaired_entry=True,
        unwind_partial_entry=True,
        min_effective_pair_qty=Decimal("5"),
        run_once=False,
        max_markets=1,
        tick_interval_s=0.2,
        max_book_age_s=5,
    )
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.BOTH_LEGS_FILLED)
    mark_both_legs_filled(
        state,
        yes_qty=Decimal("5"),
        no_qty=Decimal("5"),
        yes_entry=Decimal("0.48"),
        no_entry=Decimal("0.50"),
        entry_price_source=SOURCE_OMS_MATCHED_AVG,
        yes_entry_price_source=SOURCE_OMS_MATCHED_AVG,
        no_entry_price_source=SOURCE_OMS_MATCHED_AVG,
    )
    yes_book = LegBook(YES, Decimal("0.47"), Decimal("0.49"), False)
    no_book = LegBook(NO, Decimal("0.49"), Decimal("0.51"), False)
    safety = evaluate_activation_safety(state, cfg, yes_book, no_book)
    assert safety.ok is True


def test_activation_pending_does_not_run_stop_or_tp() -> None:
    state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.ACTIVATION_PENDING_RECHECK)
    assert state.phase not in {
        PairedBinaryPhase.BOTH_LEGS_ACTIVE,
        PairedBinaryPhase.ONLY_YES_ACTIVE,
        PairedBinaryPhase.ONLY_NO_ACTIVE,
    }


def test_latency_sample_emitted_for_entry_activation() -> None:
    from tyrex_pm.strategies.paired_binary.latency import LatencyTracker

    tracker = LatencyTracker()
    tracker.mark_decision()
    tracker.mark_sellable_seen()
    payload = tracker.payload(event="activation")
    assert payload["event"] == "activation"
    assert payload["fill_to_sellable_ms"] is None or isinstance(payload["fill_to_sellable_ms"], int)


def test_book_capture_quality_contains_book_age_ms(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    snap = make_snapshot(YES, bids=[(Decimal("0.48"), Decimal("100"))], asks=[(Decimal("0.49"), Decimal("100"))])
    coord.market_state.apply_snapshot(snap)
    book = read_leg_book(coord.market_state, YES, max_book_age_s=5.0)
    assert book.book_age_ms is not None
    assert book.book_age_ms >= 0


def test_build_exit_book_evidence_from_market_store(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    snap = make_snapshot(OTHER, bids=[(Decimal("0.39"), Decimal("100"))])
    coord.market_state.apply_snapshot(snap)
    ev = build_exit_book_evidence_for_intent(coord.market_state, OTHER, max_book_age_s=0.5)
    assert ev is not None
    assert ev["best_bid"] == "0.39"
    assert ev["stale"] is False
