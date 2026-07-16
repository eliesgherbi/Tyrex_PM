"""Architecture tests: order lifecycle vs allocation lifecycle (Phase 4.6 fix)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId, VenueOrderId
from tyrex_pm.core.models import (
    ApprovedIntent,
    EnterIntent,
    ExitIntent,
    RiskContext,
    TradeFillRecord,
    URGENCY_URGENT,
    WalletPosition,
)
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.models import ExecutionPlan
from tyrex_pm.execution.order_lifecycle import ack_submit, register_submit
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.risk.planned_order import validate_planned_order
from tyrex_pm.runtime.allocation_runtime import fill_qty_for_allocation, maybe_apply_allocation_buy
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.entry_qty_reconcile import reconcile_leg_entry_qty, reconcile_pair_entry_qty
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.runtime.paired_binary_run import _try_early_entry_completion
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import LocalOrder, OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.execution.adapters import ShadowOMS

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"
TOKEN = TokenId(YES)


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
        },
    }
    base.update(over)
    return base


def _minimal_app(**risk_over):
    return parse_app_config(
        risk=_risk(**risk_over),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "allocation_ledger": {"clamp_grace_s_after_buy": 90},
        },
    )


def _paired_app(**pb_over):
    pb = {
        "owner_id": "paired_binary",
        "market_id": "m1",
        "yes_token_id": YES,
        "no_token_id": NO,
        "position_size": "5",
        "min_effective_pair_qty": "5",
        "use_fixture_book": True,
    }
    pb.update(pb_over)
    return parse_app_config(
        risk=_risk(),
        strategy={"kind": "paired_binary", "enabled": True, "paired_binary": pb},
        runtime={
            "execution_mode": "shadow",
            "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
            "allocation_ledger": {"clamp_grace_s_after_buy": 90},
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
        },
    )


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(
        wallet=WalletStore(),
        orders=OrderStore(),
        health=HealthRuntime(),
        allocation_clamp_grace_s=90.0,
    )
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    coord.allocation_ledger_run_id = str(uuid4())
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def _approved_buy(size: str = "5") -> ApprovedIntent:
    intent = EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal(size),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    return ApprovedIntent(intent=intent, client_order_id=ClientOrderId("cid-buy-1"), run_id=RunId("r1"))


def test_resting_buy_does_not_apply_allocation(tmp_path: Path) -> None:
    app = _minimal_app()
    coord = _coord(tmp_path)
    ap = _approved_buy()
    match_evidence = {"match_status": "live"}
    qty = fill_qty_for_allocation(ap.intent, match_evidence, ap.intent.size)
    assert qty == Decimal("0")
    runs = tmp_path / "runs"
    runs.mkdir()
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        maybe_apply_allocation_buy(
            coord,
            app,
            strategy=object(),
            ap=ap,
            match_evidence=match_evidence,
            correlation_id="c1",
            intent_extensions={"allocation_owner_id": "paired_binary"},
            run_id=str(uuid4()),
        )
    assert coord.allocation_ledger.get_allocated("paired_binary", TOKEN) == Decimal("0")
    lines = (runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    payload = json.loads(lines[-1])["payload"]
    assert payload["event"] == "allocation_buy_skipped_unfilled_order"


def test_matched_buy_applies_only_filled_qty(tmp_path: Path) -> None:
    app = _minimal_app()
    coord = _coord(tmp_path)
    ap = _approved_buy()
    match_evidence = {"match_status": "matched", "taking_amount": "5"}
    runs = tmp_path / "runs"
    runs.mkdir()
    with JsonlSink(runs / "facts.jsonl") as sink:
        coord.allocation_ledger_sink = sink
        maybe_apply_allocation_buy(
            coord,
            app,
            strategy=object(),
            ap=ap,
            match_evidence=match_evidence,
            correlation_id="c1",
            intent_extensions={"allocation_owner_id": "paired_binary"},
            run_id=str(uuid4()),
        )
    assert coord.allocation_ledger.get_allocated("paired_binary", TOKEN) == Decimal("5")
    payload = json.loads((runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])["payload"]
    assert payload["event"] == "allocation_buy_applied_from_fill"


def test_partial_buy_applies_only_partial_fill_qty(tmp_path: Path) -> None:
    match_evidence = {"match_status": "matched", "taking_amount": "2"}
    ap = _approved_buy()
    qty = fill_qty_for_allocation(ap.intent, match_evidence, ap.intent.size)
    assert qty == Decimal("2")


def test_confirmed_ws_repairs_allocation(tmp_path: Path) -> None:
    app = _minimal_app()
    coord = _coord(tmp_path)
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TokenId(YES),
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.49"),
            status="CONFIRMED",
            ts_utc=utc_now(),
        )
    )
    leg = reconcile_leg_entry_qty(
        coord,
        app,
        owner_id="paired_binary",
        token_id=TokenId(YES),
        leg="yes",
        pair_correlation_id="pair1",
    )
    assert leg.effective_qty == Decimal("5")
    assert leg.source == "user_ws"


def test_allocation_buy_applied_requires_fill_evidence(tmp_path: Path) -> None:
    resting = {"match_status": "live"}
    matched = {"match_status": "matched", "taking_amount": "5"}
    ap = _approved_buy()
    assert fill_qty_for_allocation(ap.intent, resting, ap.intent.size) == Decimal("0")
    assert fill_qty_for_allocation(ap.intent, matched, ap.intent.size) == Decimal("5")


def test_orderstore_records_resting_order_without_allocation(tmp_path: Path) -> None:
    coord = _coord(tmp_path)
    ap = _approved_buy()
    register_submit(coord.orders, ap)
    ack_submit(
        coord.orders,
        ap,
        VenueOrderId("vid-1"),
        shadow_instant_fill=False,
        ack_status="live",
        filled_qty=None,
    )
    lo = coord.orders.orders[ap.client_order_id]
    assert lo.ack_status == "live"
    assert lo.size_matched == Decimal("0")
    assert coord.allocation_ledger.get_allocated("paired_binary", TOKEN) == Decimal("0")


def test_paired_entry_does_not_activate_when_one_leg_resting(tmp_path: Path) -> None:
    app = _paired_app()
    coord = _coord(tmp_path)
    ledger = coord.allocation_ledger
    ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    # NO leg only has resting order, no fill
    cid_no = ClientOrderId("no-rest")
    coord.orders.orders[cid_no] = LocalOrder(
        client_order_id=cid_no,
        venue_order_id=None,
        token_id=TokenId(NO),
        side=Side.BUY,
        remaining=Decimal("5"),
        original_size=Decimal("5"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=utc_now(),
        last_local_source="local",
        submit_fingerprint=None,
        ack_status="live",
        limit_price=Decimal("0.33"),
        register_utc=utc_now(),
    )
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TokenId(YES),
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.68"),
            status="MATCHED",
            ts_utc=utc_now(),
        )
    )
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pair1",
        yes_token_id=YES,
        no_token_id=NO,
        owner_id="paired_binary",
        market_id="m1",
    )
    state.yes.entry_client_order_id = "yes-cid"
    state.no.entry_client_order_id = str(cid_no)
    pair = reconcile_pair_entry_qty(
        coord,
        app,
        owner_id="paired_binary",
        yes_token_id=YES,
        no_token_id=NO,
        pair_correlation_id="pair1",
        yes_client_order_id="yes-cid",
        no_client_order_id=str(cid_no),
    )
    assert pair.yes.effective_qty == Decimal("5")
    assert pair.no.effective_qty == Decimal("0")
    assert pair.effective_pair_qty == Decimal("0")


@pytest.mark.asyncio
async def test_paired_monitor_does_not_start_until_both_legs_filled(tmp_path: Path) -> None:
    app = _paired_app()
    coord = _coord(tmp_path)
    ledger = coord.allocation_ledger
    ledger.apply_buy("paired_binary", TokenId(YES), Decimal("5"), correlation_id="y")
    coord.wallet.trade_fill_records.append(
        TradeFillRecord(
            token_id=TokenId(YES),
            side=Side.BUY,
            size=Decimal("5"),
            price=Decimal("0.68"),
            status="MATCHED",
            ts_utc=utc_now(),
        )
    )
    state = PairedBinaryRuntimeState(
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pair1",
        yes_token_id=YES,
        no_token_id=NO,
        owner_id="paired_binary",
        market_id="m1",
    )
    completed = await _try_early_entry_completion(
        app=app,
        run_id=RunId("r1"),
        coord=coord,
        sink=None,
        oms=ShadowOMS(),
        strategy=object(),
        cfg=app.paired_binary,
        state=state,
        apply_local_shadow_fill=True,
        live_clob_client=None,
    )
    assert completed is False
    assert state.phase == PairedBinaryPhase.BOTH_ENTRY_PENDING


def test_urgent_reduce_only_sell_allows_missing_mark_with_fresh_bid() -> None:
    app = parse_app_config(
        risk=_risk(),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime={"execution_mode": "shadow", "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"}},
    )
    no_mark = TokenId("999")
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(
            WalletPosition(token_id=no_mark, qty=Decimal("5"), avg_price_usd=None),
        ),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=utc_now(),
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
    )
    intent = ExitIntent(
        token_id=no_mark,
        side=Side.SELL,
        size=Decimal("5"),
        limit_price=Decimal("0.40"),
        order_style=OrderStyle.GTC,
        urgency=URGENCY_URGENT,
    )
    plan = ExecutionPlan(
        intent=intent,
        client_order_id=ClientOrderId("sell-1"),
        run_id=RunId("r1"),
        planner_reason="planner_urgent_exit_fak",
        urgency=URGENCY_URGENT,
        book_evidence={"best_bid": "0.39", "stale": False},
    )
    decision = validate_planned_order(plan, ctx, app=app)
    assert decision.approved is True
    assert decision.extensions.get("reduce_only") is True
    assert decision.extensions.get("mark_source") == "executable_bid"


def test_urgent_reduce_only_sell_denies_missing_mark_without_fresh_bid() -> None:
    app = parse_app_config(
        risk=_risk(),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime={"execution_mode": "shadow", "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"}},
    )
    no_mark = TokenId("999")
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(
            WalletPosition(token_id=no_mark, qty=Decimal("5"), avg_price_usd=None),
        ),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=utc_now(),
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
    )
    intent = ExitIntent(
        token_id=no_mark,
        side=Side.SELL,
        size=Decimal("5"),
        limit_price=Decimal("0.40"),
        order_style=OrderStyle.GTC,
        urgency=URGENCY_URGENT,
    )
    plan = ExecutionPlan(
        intent=intent,
        client_order_id=ClientOrderId("sell-1"),
        run_id=RunId("r1"),
        planner_reason="planner_urgent_exit_fak",
        urgency=URGENCY_URGENT,
        book_evidence={},
    )
    decision = validate_planned_order(plan, ctx, app=app)
    assert decision.approved is False
    assert "deployment_mark_unknown" in decision.reason_codes


def test_non_reduce_only_sell_still_denies_missing_mark() -> None:
    app = parse_app_config(
        risk=_risk(),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime={"execution_mode": "shadow", "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"}},
    )
    no_mark = TokenId("999")
    ctx = RiskContext(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(
            WalletPosition(token_id=no_mark, qty=Decimal("2"), avg_price_usd=None),
        ),
        open_orders=(),
        usdc_balance=Decimal("1000"),
        usdc_allowance=Decimal("1000"),
        last_wallet_sync_ts=utc_now(),
        mark_prices={},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
    )
    intent = ExitIntent(
        token_id=no_mark,
        side=Side.SELL,
        size=Decimal("5"),
        limit_price=Decimal("0.40"),
        order_style=OrderStyle.GTC,
        urgency=URGENCY_URGENT,
    )
    plan = ExecutionPlan(
        intent=intent,
        client_order_id=ClientOrderId("sell-1"),
        run_id=RunId("r1"),
        planner_reason="planner_urgent_exit_fak",
        urgency=URGENCY_URGENT,
        book_evidence={"best_bid": "0.39"},
    )
    decision = validate_planned_order(plan, ctx, app=app)
    assert decision.approved is False


def test_recovery_exiting_no_flat_transitions_done(tmp_path: Path) -> None:
    app = _paired_app()
    coord = _coord(tmp_path)
    state_dir = tmp_path / "state"
    path = state_dir / "paired_binary" / "paired_binary" / "m1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "phase": "EXITING_NO",
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
            }
        ),
        encoding="utf-8",
    )
    state = recover_on_startup(coord, app.paired_binary, state_dir=state_dir)
    assert state.phase == PairedBinaryPhase.DONE


def test_recovery_exiting_no_position_no_open_order_transitions_only_no_active(tmp_path: Path) -> None:
    app = _paired_app()
    coord = _coord(tmp_path)
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.33")
    )
    state_dir = tmp_path / "state"
    path = state_dir / "paired_binary" / "paired_binary" / "m1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "phase": "EXITING_NO",
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
            }
        ),
        encoding="utf-8",
    )
    state = recover_on_startup(coord, app.paired_binary, state_dir=state_dir)
    assert state.phase == PairedBinaryPhase.STOP_PENDING_NO


def test_recovery_exiting_no_with_open_sell_keeps_exiting(tmp_path: Path) -> None:
    app = _paired_app()
    coord = _coord(tmp_path)
    coord.wallet.positions[TokenId(NO)] = WalletPosition(
        token_id=TokenId(NO), qty=Decimal("5"), avg_price_usd=Decimal("0.33")
    )
    cid = ClientOrderId("sell-no")
    coord.orders.orders[cid] = LocalOrder(
        client_order_id=cid,
        venue_order_id=None,
        token_id=TokenId(NO),
        side=Side.SELL,
        remaining=Decimal("5"),
        original_size=Decimal("5"),
        size_matched=Decimal("0"),
        confirmation="provisional",
        submit_ack_utc=utc_now(),
        last_local_source="local",
        submit_fingerprint=None,
        ack_status="live",
        limit_price=Decimal("0.30"),
        register_utc=utc_now(),
    )
    state_dir = tmp_path / "state"
    path = state_dir / "paired_binary" / "paired_binary" / "m1.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "phase": "EXITING_NO",
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": YES,
                "no_token_id": NO,
            }
        ),
        encoding="utf-8",
    )
    state = recover_on_startup(coord, app.paired_binary, state_dir=state_dir)
    assert state.phase == PairedBinaryPhase.EXITING_NO
