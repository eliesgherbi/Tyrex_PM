"""P3.5 exit lifecycle: arming, facts, completion semantics, partial fills."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.ingestion.user_stream import apply_user_ws_message
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_EXIT_LIFECYCLE,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RISK,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.exit_lifecycle import (
    clamp_planned_sell_size,
    parse_oms_match_evidence,
    required_sell_qty,
)
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import (
    process_intent_work_unit,
    process_scheduled_exit_demo_due,
    refresh_positions_immediate_and_try_arm,
)
from tyrex_pm.runtime.config import (
    SellTestBuyConfig,
    SellTestSellConfig,
    SellTestStrategyConfig,
    ShadowBootstrapConfig,
    parse_app_config,
)
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.sell_test.strategy import SellTestState, SellTestStrategy, try_arm_sell_test_pending


_BASE_LIVE_RUNTIME = {
    "execution_mode": "live",
    "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
    "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
    "logging": {"level": "WARNING"},
}


class _MatchedLiveOMS:
    """Minimal OMS stub returning a matched live ack JSON string."""

    async def submit(self, ap, *, market_info=None) -> str:
        del market_info
        size = ap.intent.size
        return json.dumps(
            {
                "status": "matched",
                "takingAmount": str(size),
                "makingAmount": "5",
                "orderID": "0xmatched-test-order",
                "success": True,
            }
        )

    async def cancel(self, ac) -> str:
        del ac
        return "cancel_ack"


def _live_sell_test_app(token_id: str = "tok-live-p351") -> object:
    return parse_app_config(
        risk=dict(_BASE_RISK),
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": token_id,
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_LIVE_RUNTIME),
    )


def _wire_live_sell_test_coord(
    strat: SellTestStrategy,
    coord: RuntimeCoordinator,
) -> None:
    coord.health.first_v2_sync_complete = True
    coord.health.clob_session_ok = True
    coord.health.heartbeat_ok = True
    coord.scheduled_exit_demo_try_arm = lambda *, source="post_buy_ack": try_arm_sell_test_pending(
        strat, coord, source=source
    )
    coord.positions_client = object()
    coord.positions_wallet_address = "0xwallet"


def _exit_lifecycle_rows(tmp_path: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def _event_indices(rows: list[dict], event: str) -> list[int]:
    return [
        i
        for i, r in enumerate(rows)
        if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE and r["payload"].get("event") == event
    ]


def _bootstrap_wallet(coord: RuntimeCoordinator) -> None:
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )


def _arm_sources(rows: list[dict]) -> list[str]:
    out: list[str] = []
    for r in rows:
        if r["fact_type"] != FACT_TYPE_EXIT_LIFECYCLE:
            continue
        payload = r["payload"]
        if payload.get("event") in ("arm_attempt", "arm_granted", "waiting_for_inventory"):
            src = payload.get("source")
            if src is not None:
                out.append(str(src))
    return out


_BASE_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
    "inventory": {"sell_requires_venue_position": True},
    "concurrency": {"max_orders_in_flight": 8},
    "readiness": {
        "require_wallet_sync": False,
        "max_wallet_age_s_live": 120,
        "require_heartbeat_live": False,
        "require_user_ws_live": False,
    },
}

_BASE_RUNTIME = {
    "execution_mode": "shadow",
    "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
    "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
    "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
    "logging": {"level": "WARNING"},
}


def _coord_with_sink(tmp_path: Path) -> tuple[RuntimeCoordinator, JsonlSink, str]:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    run_id = str(uuid4())
    sink = JsonlSink(tmp_path / "facts.jsonl")
    sink.__enter__()
    coord.exit_lifecycle_run_id = run_id
    coord.exit_lifecycle_sink = sink
    return coord, sink, run_id


def _sell_test_cfg(token_id: str = "tok-p35", delay_s: float = 0.0) -> SellTestStrategyConfig:
    return SellTestStrategyConfig(
        enabled=True,
        token_id=token_id,
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.5"),
            order_style=OrderStyle.GTC,
        ),
        sell=SellTestSellConfig(
            enabled=True, delay_s=delay_s, order_style=OrderStyle.GTC, limit_price=None
        ),
        run_once=True,
    )


def test_parse_oms_match_evidence_matched() -> None:
    raw = json.dumps(
        {
            "status": "matched",
            "takingAmount": "23.52",
            "makingAmount": "3.95",
            "orderID": "0xabc",
        }
    )
    ev = parse_oms_match_evidence(raw)
    assert ev["match_status"] == "matched"
    assert ev["taking_amount"] == "23.52"
    assert ev["order_id"] == "0xabc"


def test_partial_fill_clamps_planned_sell() -> None:
    planned = Decimal("10")
    taking = Decimal("6")
    req = required_sell_qty(planned, taking)
    assert req == Decimal("6")
    clamped = clamp_planned_sell_size(planned, match_taking_amount=taking, available=Decimal("8"))
    assert clamped == Decimal("6")


def test_ws_confirmed_updates_position_and_triggers_arm(tmp_path: Path) -> None:
    coord, sink, _run_id = _coord_with_sink(tmp_path)
    cfg = _sell_test_cfg(token_id="tok-ws")
    state = SellTestState(cfg)
    tid = TokenId("tok-ws")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("c1"), run_id=RunId("r1"))
    state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-ws",
        execution_mode=ExecutionMode.LIVE,
        apply_shadow_fill=False,
    )
    armed: list[str] = []

    def _try_arm(*, source="post_buy_ack"):
        armed.append(source)
        state.try_arm_live_pending(coord, source=source)

    coord.scheduled_exit_demo_try_arm = _try_arm
    apply_user_ws_message(
        coord.wallet,
        {
            "type": "TRADE",
            "asset_id": "tok-ws",
            "side": "BUY",
            "size": "10",
            "price": "0.5",
            "status": "CONFIRMED",
        },
    )
    _try_arm(source="websocket")
    assert len(state._armed) == 1
    assert "websocket" in armed
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_immediate_positions_refresh_triggers_arm(tmp_path: Path) -> None:
    coord, sink, run_id = _coord_with_sink(tmp_path)
    cfg = _sell_test_cfg(token_id="tok-rest")
    state = SellTestState(cfg)
    tid = TokenId("tok-rest")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("c2"), run_id=RunId("r2"))
    state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-rest",
        execution_mode=ExecutionMode.LIVE,
        apply_shadow_fill=False,
    )
    coord.scheduled_exit_demo_try_arm = lambda *, source="post_buy_ack": state.try_arm_live_pending(
        coord, source=source
    )
    coord.positions_client = object()
    coord.positions_wallet_address = "0xwallet"

    async def _fake_refresh(wallet, client, addr):
        wallet.positions[tid] = WalletPosition(
            token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
        )
        return True

    with patch(
        "tyrex_pm.runtime.pipeline.refresh_positions_from_data_api",
        new=AsyncMock(side_effect=_fake_refresh),
    ):
        ok = await refresh_positions_immediate_and_try_arm(coord, sink, run_id)
    assert ok is True
    assert len(state._armed) == 1
    sink.__exit__(None, None, None)


def test_is_done_false_until_sell_terminal(tmp_path: Path) -> None:
    coord, sink, _ = _coord_with_sink(tmp_path)
    strat = SellTestStrategy(_sell_test_cfg(delay_s=0))
    strat.notify_buy_submitted()
    ent = EnterIntent(
        token_id=TokenId("tok-p35"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("c3"), run_id=RunId("r3"))
    strat.sell_test_state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-x",
        execution_mode=ExecutionMode.SHADOW,
        apply_shadow_fill=True,
    )
    assert strat.sell_test_state.pop_due_rows(coord)
    assert not strat.is_done()
    strat.sell_test_state.mark_sell_in_flight()
    assert not strat.is_done()
    strat.sell_test_state.mark_sell_terminal("sell_submitted")
    assert strat.is_done()
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_buy_risk_denied_allows_retry_and_no_pending_sell(tmp_path: Path) -> None:
    app = parse_app_config(
        risk={
            **_BASE_RISK,
            "notional": {"min_usd": "1000", "max_usd": "100000", "max_policy": "deny"},
        },
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": "tok-deny",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id = _coord_with_sink(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    wu = strat.initial_buy_work_units()[0]
    await process_intent_work_unit(
        wu,
        app=app,
        run_id=RunId(run_id),
        strategy=strat,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
        apply_local_shadow_fill=True,
    )
    assert not strat.buy_submit_succeeded
    assert strat.sell_test_state._pending_live == []
    assert strat.initial_buy_work_units() != []
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_timeout_waiting_for_inventory_emits_fact(tmp_path: Path) -> None:
    coord, sink, _ = _coord_with_sink(tmp_path)
    state = SellTestState(_sell_test_cfg(token_id="tok-to"))
    ent = EnterIntent(
        token_id=TokenId("tok-to"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("c4"), run_id=RunId("r4"))
    state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-to",
        execution_mode=ExecutionMode.LIVE,
        apply_shadow_fill=False,
    )
    state.emit_timeout_waiting_for_inventory(coord)
    assert state.is_terminal
    rows = [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    timeout_rows = [
        r
        for r in rows
        if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE
        and r["payload"]["event"] == "timeout_waiting_for_sellable_inventory"
    ]
    assert len(timeout_rows) == 1
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_shadow_golden_exit_lifecycle_chain(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy={
            "kind": "sell_test",
            "enabled": True,
            "token_id": "tok-e2e",
            "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.5"},
            "sell": {"enabled": True, "delay_s": 0},
            "run_once": True,
        },
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id = _coord_with_sink(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    for wu in strat.initial_buy_work_units():
        await process_intent_work_unit(
            wu,
            app=app,
            run_id=RunId(run_id),
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )
    await asyncio.sleep(0.05)
    await process_scheduled_exit_demo_due(
        strategy=strat,
        app=app,
        run_id=RunId(run_id),
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
        apply_local_shadow_fill=True,
        live_clob_client=None,
    )
    rows = [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    events = [r["payload"]["event"] for r in rows if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE]
    assert "pending_registered" in events
    assert "arm_granted" in events
    assert "sell_due" in events
    assert "sell_intent_emitted" in events
    assert "sell_submitted" in events
    assert "sell_completed" in events
    assert strat.is_done()
    assert any(r["fact_type"] == FACT_TYPE_OMS_SUBMIT for r in rows)
    assert sum(1 for r in rows if r["fact_type"] == FACT_TYPE_INTENT and r["payload"]["side"] == "SELL") == 1
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_live_matched_buy_registers_pending_before_immediate_try_arm(
    tmp_path: Path,
) -> None:
    app = _live_sell_test_app(token_id="tok-order-p351")
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id = _coord_with_sink(tmp_path)
    _wire_live_sell_test_coord(strat, coord)
    _bootstrap_wallet(coord)
    refresh_order: list[str] = []

    async def _fake_refresh(wallet, client, addr):
        refresh_order.append("refresh")
        assert len(strat.sell_test_state._pending_live) == 1
        return True

    with patch(
        "tyrex_pm.runtime.pipeline.refresh_positions_from_data_api",
        new=AsyncMock(side_effect=_fake_refresh),
    ):
        await process_intent_work_unit(
            strat.initial_buy_work_units()[0],
            app=app,
            run_id=RunId(run_id),
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=_MatchedLiveOMS(),
            apply_local_shadow_fill=False,
        )
    rows = _exit_lifecycle_rows(tmp_path)
    pending_idx = _event_indices(rows, "pending_registered")[0]
    immediate_arm_idxs = [
        i
        for i, r in enumerate(rows)
        if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE
        and r["payload"].get("source") == "immediate_positions_refresh"
    ]
    assert refresh_order == ["refresh"]
    assert immediate_arm_idxs, "expected immediate_positions_refresh arm lifecycle fact"
    assert pending_idx < immediate_arm_idxs[0]
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_immediate_refresh_emits_arm_attempt_source_after_registration(
    tmp_path: Path,
) -> None:
    app = _live_sell_test_app(token_id="tok-arm-src")
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    coord, sink, run_id = _coord_with_sink(tmp_path)
    _wire_live_sell_test_coord(strat, coord)
    _bootstrap_wallet(coord)

    with patch(
        "tyrex_pm.runtime.pipeline.refresh_positions_from_data_api",
        new=AsyncMock(return_value=True),
    ):
        await process_intent_work_unit(
            strat.initial_buy_work_units()[0],
            app=app,
            run_id=RunId(run_id),
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=_MatchedLiveOMS(),
            apply_local_shadow_fill=False,
        )
    sources = _arm_sources(_exit_lifecycle_rows(tmp_path))
    assert "immediate_positions_refresh" in sources
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_immediate_refresh_arm_granted_when_positions_visible(
    tmp_path: Path,
) -> None:
    app = _live_sell_test_app(token_id="tok-arm-now")
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    tid = TokenId("tok-arm-now")
    coord, sink, run_id = _coord_with_sink(tmp_path)
    _wire_live_sell_test_coord(strat, coord)
    _bootstrap_wallet(coord)

    async def _fake_refresh(wallet, client, addr):
        wallet.positions[tid] = WalletPosition(
            token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
        )
        return True

    with patch(
        "tyrex_pm.runtime.pipeline.refresh_positions_from_data_api",
        new=AsyncMock(side_effect=_fake_refresh),
    ):
        await process_intent_work_unit(
            strat.initial_buy_work_units()[0],
            app=app,
            run_id=RunId(run_id),
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=_MatchedLiveOMS(),
            apply_local_shadow_fill=False,
        )
    rows = _exit_lifecycle_rows(tmp_path)
    granted = [
        r
        for r in rows
        if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE
        and r["payload"].get("event") == "arm_granted"
        and r["payload"].get("source") == "immediate_positions_refresh"
    ]
    assert len(granted) == 1
    assert granted[0]["payload"]["armed"] is True
    assert len(strat.sell_test_state._armed) == 1
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_immediate_refresh_stale_positions_then_websocket_arms(
    tmp_path: Path,
) -> None:
    app = _live_sell_test_app(token_id="tok-ws-fallback")
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    tid = TokenId("tok-ws-fallback")
    coord, sink, run_id = _coord_with_sink(tmp_path)
    _wire_live_sell_test_coord(strat, coord)
    _bootstrap_wallet(coord)

    with patch(
        "tyrex_pm.runtime.pipeline.refresh_positions_from_data_api",
        new=AsyncMock(return_value=True),
    ):
        await process_intent_work_unit(
            strat.initial_buy_work_units()[0],
            app=app,
            run_id=RunId(run_id),
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=_MatchedLiveOMS(),
            apply_local_shadow_fill=False,
        )
    rows_after_buy = _exit_lifecycle_rows(tmp_path)
    immediate_waiting = [
        r
        for r in rows_after_buy
        if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE
        and r["payload"].get("source") == "immediate_positions_refresh"
        and r["payload"].get("event") in ("arm_attempt", "waiting_for_inventory")
        and r["payload"].get("armed") is False
    ]
    assert immediate_waiting, "stale REST should emit waiting arm_attempt"
    assert len(strat.sell_test_state._armed) == 0

    apply_user_ws_message(
        coord.wallet,
        {
            "type": "TRADE",
            "asset_id": str(tid),
            "side": "BUY",
            "size": "10",
            "price": "0.5",
            "status": "CONFIRMED",
        },
    )
    try_arm_sell_test_pending(strat, coord, source="websocket")
    assert len(strat.sell_test_state._armed) == 1
    ws_granted = [
        r
        for r in _exit_lifecycle_rows(tmp_path)
        if r["fact_type"] == FACT_TYPE_EXIT_LIFECYCLE
        and r["payload"].get("event") == "arm_granted"
        and r["payload"].get("source") == "websocket"
    ]
    assert len(ws_granted) == 1
    sink.__exit__(None, None, None)
