"""Tests for the tp_sl_test validation harness (P6)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, ExitIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH, FACT_TYPE_INTENT
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    TpSlTestExitConfig,
    TpSlTestStrategyConfig,
    parse_app_config,
)
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.sell_test.pricing import ResolvedPrice
from tyrex_pm.runtime.allocation_ids import TP_SL_TEST_INTENT_SOURCE
from tyrex_pm.strategies.tp_sl_test.strategy import (
    TpSlTestState,
    TpSlTestStrategy,
    resolve_entry_price,
)


_BASE_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "100", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "500", "portfolio_cap_usd": "5000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
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


def _tp_sl_strategy_dict(
    *,
    token_id: str = "tok-tpsl",
    fixture_prices: list[str] | None = None,
    take_profit: str | None = "0.60",
    stop_loss: str | None = "0.40",
    take_profit_pct: str | None = None,
    stop_loss_pct: str | None = None,
    trigger_reference: str | None = None,
    size_mode: str = "full_allocated_position",
    fixed_size: str | None = None,
) -> dict:
    exit_block: dict = {
        "enabled": True,
        "size_mode": size_mode,
        "percent": "1.0",
        "order_style": "GTC",
        "pricing_mode": "fixed",
        "limit_price": "0.49",
    }
    if fixed_size is not None:
        exit_block["fixed_size"] = fixed_size
    monitor: dict = {
        "enabled": True,
        "price_source": "fixture",
        "poll_interval_s": 0.01,
        "trigger_mode": "take_profit_or_stop_loss",
        "fixture_prices": fixture_prices or ["0.50", "0.55", "0.61"],
    }
    if take_profit_pct is not None:
        monitor["take_profit_pct"] = take_profit_pct
    elif take_profit is not None:
        monitor["take_profit_price"] = take_profit
    if stop_loss_pct is not None:
        monitor["stop_loss_pct"] = stop_loss_pct
    elif stop_loss is not None:
        monitor["stop_loss_price"] = stop_loss
    if trigger_reference is not None:
        monitor["trigger_reference"] = trigger_reference
    return {
        "kind": "tp_sl_test",
        "enabled": True,
        "token_id": token_id,
        "owner_id": "tp_sl_test",
        "buy": {
            "enabled": True,
            "notional_usd": "5",
            "limit_price": "0.50",
            "order_style": "GTC",
        },
        "monitor": monitor,
        "exit": exit_block,
        "timeouts": {
            "inventory_timeout_s": 90,
            "trigger_timeout_s": 120,
            "completion_timeout_s": 120,
        },
        "run_once": True,
    }


def _make_cfg(**overrides: object) -> TpSlTestStrategyConfig:
    raw = _tp_sl_strategy_dict(**overrides)  # type: ignore[arg-type]
    app = parse_app_config(risk=dict(_BASE_RISK), strategy=raw, runtime=dict(_BASE_RUNTIME))
    assert app.tp_sl_test is not None
    return app.tp_sl_test


def _coord_with_ledger(tmp_path: Path | None = None) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    path = (tmp_path / "alloc.json") if tmp_path else None
    coord.allocation_ledger = AllocationLedger(path=path)
    return coord


def _wire_sink(coord: RuntimeCoordinator, sink: JsonlSink, run_id: RunId) -> None:
    coord.exit_lifecycle_sink = sink
    coord.exit_lifecycle_run_id = str(run_id)
    coord.allocation_ledger_sink = sink
    coord.allocation_ledger_run_id = str(run_id)


def _buy_ack(
    strat: TpSlTestStrategy,
    coord: RuntimeCoordinator,
    *,
    size: Decimal = Decimal("10"),
    correlation_id: str = "corr-buy",
    credit_allocation: bool = True,
    allocation_qty: Decimal | None = None,
) -> None:
    tid = TokenId(strat.cfg.token_id)
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=size,
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid-buy"), run_id=RunId("r1"))
    strat.on_buy_submit_ack(
        ap=ap,
        parent_correlation_id=correlation_id,
        coord=coord,
        execution_mode=ExecutionMode.SHADOW,
        apply_local_shadow_fill=True,
        match_evidence={"taking_amount": str(size)},
    )
    if not credit_allocation or coord.allocation_ledger is None:
        return
    qty = allocation_qty if allocation_qty is not None else size
    owner = strat.cfg.owner_id
    if coord.allocation_ledger.get_available_allocated(owner, tid) <= 0:
        coord.allocation_ledger.apply_buy(owner, tid, qty, correlation_id=correlation_id)
    pos = coord.wallet.positions.get(tid)
    if pos is None:
        coord.wallet.positions[tid] = WalletPosition(
            token_id=tid,
            qty=qty,
            avg_price_usd=Decimal("0.5"),
        )
    elif pos.qty < qty:
        pos.qty = qty
    strat.tp_sl_state.try_promote_inventory(coord, source="post_buy_ack")


def _health_events(rows: list[dict], event: str) -> list[dict]:
    return [
        r
        for r in rows
        if r.get("fact_type") == FACT_TYPE_HEALTH and r.get("payload", {}).get("event") == event
    ]


def test_parse_tp_sl_test_strategy_yaml() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_tp_sl_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.tp_sl_test is not None
    assert app.tp_sl_test.token_id == "tok-tpsl"
    assert app.tp_sl_test.owner_id == "tp_sl_test"
    assert app.tp_sl_test.buy.notional_usd == Decimal("5")
    assert app.tp_sl_test.monitor.fixture_prices == (
        Decimal("0.50"),
        Decimal("0.55"),
        Decimal("0.61"),
    )
    assert app.tp_sl_test.monitor.take_profit_price == Decimal("0.60")


def test_parse_tp_sl_test_missing_token_id() -> None:
    bad = _tp_sl_strategy_dict()
    bad["token_id"] = ""
    with pytest.raises(ConfigError, match="token_id"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_tp_sl_test_missing_tp_sl_prices() -> None:
    bad = _tp_sl_strategy_dict(take_profit=None, stop_loss=None)
    with pytest.raises(ConfigError, match="take_profit_price, take_profit_pct"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_tp_sl_test_rejects_both_tp_price_and_pct() -> None:
    bad = _tp_sl_strategy_dict()
    bad["monitor"]["take_profit_pct"] = "0.20"
    with pytest.raises(ConfigError, match="take_profit_price and take_profit_pct"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_tp_sl_test_rejects_both_sl_price_and_pct() -> None:
    bad = _tp_sl_strategy_dict()
    bad["monitor"]["stop_loss_pct"] = "0.10"
    with pytest.raises(ConfigError, match="stop_loss_price and stop_loss_pct"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_tp_sl_test_pct_only() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_tp_sl_strategy_dict(
            take_profit=None,
            stop_loss=None,
            take_profit_pct="0.20",
            stop_loss_pct="0.10",
            trigger_reference="entry_price",
        ),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.tp_sl_test is not None
    assert app.tp_sl_test.monitor.take_profit_pct == Decimal("0.20")
    assert app.tp_sl_test.monitor.stop_loss_pct == Decimal("0.10")
    assert app.tp_sl_test.monitor.trigger_reference == "entry_price"


def test_parse_tp_sl_test_rejects_stop_loss_pct_ge_one() -> None:
    bad = _tp_sl_strategy_dict(
        take_profit=None,
        stop_loss=None,
        take_profit_pct="0.20",
        stop_loss_pct="1.0",
    )
    with pytest.raises(ConfigError, match="stop_loss_pct must be < 1"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_compute_thresholds_from_entry_price() -> None:
    from tyrex_pm.strategies.tp_sl_test.strategy import compute_tp_sl_trigger_thresholds

    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_tp_sl_strategy_dict(
            take_profit=None,
            stop_loss=None,
            take_profit_pct="0.20",
            stop_loss_pct="0.10",
        ),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.tp_sl_test is not None
    tp, sl, evidence = compute_tp_sl_trigger_thresholds(
        app.tp_sl_test.monitor,
        Decimal("0.50"),
    )
    assert tp == Decimal("0.60")
    assert sl == Decimal("0.45")
    assert evidence["reference_price"] == "0.50"
    assert Decimal(str(evidence["take_profit_trigger_price"])) == Decimal("0.60")
    assert Decimal(str(evidence["stop_loss_trigger_price"])) == Decimal("0.45")


def test_parse_tp_sl_test_decimal_fields() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_tp_sl_strategy_dict(take_profit="0.601", stop_loss="0.399"),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.tp_sl_test is not None
    assert app.tp_sl_test.monitor.take_profit_price == Decimal("0.601")
    assert app.tp_sl_test.monitor.stop_loss_price == Decimal("0.399")


def test_parse_tp_sl_test_rejects_unknown_price_source() -> None:
    bad = _tp_sl_strategy_dict()
    bad["monitor"]["price_source"] = "last_trade"
    with pytest.raises(ConfigError, match="price_source"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_tp_sl_test_rejects_mark_price_source() -> None:
    bad = _tp_sl_strategy_dict()
    bad["monitor"]["price_source"] = "mark"
    with pytest.raises(ConfigError, match="mark"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_tp_sl_test_rejects_unknown_size_mode() -> None:
    bad = _tp_sl_strategy_dict()
    bad["exit"]["size_mode"] = "all_wallet"
    with pytest.raises(ConfigError, match="size_mode"):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_tp_sl_test_buy_registers_monitor(tmp_path: Path) -> None:
    cfg = _make_cfg()
    strat = TpSlTestStrategy(cfg)
    coord = _coord_with_ledger(tmp_path)
    run_id = RunId(str(uuid4()))
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        _wire_sink(coord, sink, run_id)
        _buy_ack(strat, coord)
    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    reg = _health_events(rows, "tp_sl_registered")
    assert len(reg) == 1
    payload = reg[0]["payload"]
    assert payload["owner_id"] == "tp_sl_test"
    assert payload["lifecycle_phase"] == "position_active"
    assert payload["intended_buy_size"] == "10"
    assert Decimal(str(payload["protected_qty"])) > 0
    assert _health_events(rows, "tp_sl_position_active")
    assert strat.tp_sl_state._monitoring is not None


def test_tp_sl_pct_take_profit_triggers_exit(tmp_path: Path) -> None:
    asyncio.run(
        _trigger_exit(
            tmp_path,
            fixture_prices=["0.50", "0.55", "0.61"],
            expected_trigger="take_profit",
            use_pct=True,
        )
    )


def test_tp_sl_pct_stop_loss_triggers_exit(tmp_path: Path) -> None:
    asyncio.run(
        _trigger_exit(
            tmp_path,
            fixture_prices=["0.50", "0.48", "0.44"],
            expected_trigger="stop_loss",
            use_pct=True,
        )
    )


def test_tp_sl_pct_facts_include_threshold_evidence(tmp_path: Path) -> None:
    rows = asyncio.run(
        _trigger_exit(
            tmp_path,
            fixture_prices=["0.50", "0.61"],
            expected_trigger="take_profit",
            use_pct=True,
        )
    )
    triggered = _health_events(rows, "tp_sl_triggered")
    assert len(triggered) == 1
    payload = triggered[0]["payload"]
    assert payload["trigger_reference"] == "entry_price"
    assert payload["reference_price"] == "0.50"
    assert payload["take_profit_pct"] == "0.20"
    assert payload["stop_loss_pct"] == "0.10"
    assert Decimal(str(payload["take_profit_trigger_price"])) == Decimal("0.60")
    assert Decimal(str(payload["stop_loss_trigger_price"])) == Decimal("0.45")
    sizing = _health_events(rows, "tp_sl_exit_sizing")
    assert Decimal(str(sizing[0]["payload"]["tp_sl_sizing"]["take_profit_trigger_price"])) == Decimal(
        "0.60"
    )


async def _trigger_exit(
    tmp_path: Path,
    fixture_prices: list[str],
    expected_trigger: str,
    *,
    use_pct: bool = False,
) -> list[dict]:
    if use_pct:
        strategy_raw = _tp_sl_strategy_dict(
            fixture_prices=fixture_prices,
            take_profit=None,
            stop_loss=None,
            take_profit_pct="0.20",
            stop_loss_pct="0.10",
            trigger_reference="entry_price",
        )
    else:
        strategy_raw = _tp_sl_strategy_dict(fixture_prices=fixture_prices)
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=strategy_raw,
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.tp_sl_test is not None
    strat = TpSlTestStrategy(app.tp_sl_test)
    coord = _coord_with_ledger(tmp_path)
    assert app.runtime.shadow_bootstrap is not None
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    run_id = RunId(str(uuid4()))
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        _wire_sink(coord, sink, run_id)
        for wu in strat.initial_buy_work_units():
            await process_intent_work_unit(
                wu,
                app=app,
                run_id=run_id,
                strategy=strat,
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
                apply_local_shadow_fill=True,
            )
        while strat.tp_sl_state._monitoring is not None and not strat.tp_sl_state._monitoring.triggered:
            await strat.tp_sl_state.tick_monitor(coord, live_clob_client=None)
        work = await strat.tp_sl_state.resolve_triggered_work_units(
            coord=coord,
            live_clob_client=None,
        )
        assert len(work) == 1
        assert isinstance(work[0].intent, ExitIntent)
        assert work[0].intent.side == Side.SELL
        await process_intent_work_unit(
            work[0],
            app=app,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
        )
    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    triggered = _health_events(rows, "tp_sl_triggered")
    assert len(triggered) == 1
    assert triggered[0]["payload"]["trigger"] == expected_trigger
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT and r["payload"]["side"] == "SELL"]
    assert len(intents) == 1
    assert intents[0]["payload"]["source"] == TP_SL_TEST_INTENT_SOURCE
    return rows


def test_tp_sl_take_profit_triggers_exit(tmp_path: Path) -> None:
    asyncio.run(_trigger_exit(tmp_path, ["0.50", "0.61"], "take_profit"))


def test_tp_sl_stop_loss_triggers_exit(tmp_path: Path) -> None:
    asyncio.run(_trigger_exit(tmp_path, ["0.50", "0.39"], "stop_loss"))


async def _run_monitor_until_triggered(st: TpSlTestState, coord: RuntimeCoordinator) -> None:
    while st._monitoring is not None and not st._monitoring.triggered:
        await st.tick_monitor(coord, live_clob_client=None)


def test_tp_sl_clamps_to_allocation(tmp_path: Path) -> None:
    cfg = _make_cfg()
    strat = TpSlTestStrategy(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("100"), avg_price_usd=Decimal("0.5"))
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("6"), correlation_id="c1")
    run_id = RunId(str(uuid4()))

    async def _run() -> None:
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            _wire_sink(coord, sink, run_id)
            _buy_ack(strat, coord, size=Decimal("100"), credit_allocation=False)
            await _run_monitor_until_triggered(strat.tp_sl_state, coord)

    asyncio.run(_run())
    assert strat.tp_sl_state._monitoring.final_size == Decimal("6")


def test_tp_sl_blocks_zero_allocation(tmp_path: Path) -> None:
    cfg = _make_cfg()
    strat = TpSlTestStrategy(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    run_id = RunId(str(uuid4()))

    async def _run() -> None:
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            _wire_sink(coord, sink, run_id)
            _buy_ack(strat, coord, credit_allocation=False)
            work = await strat.tp_sl_state.resolve_triggered_work_units(
                coord=coord, live_clob_client=None
            )
            assert work == []
            assert strat.tp_sl_state._monitoring is None
            assert len(strat.tp_sl_state._pending_inventory) == 1

    asyncio.run(_run())


def test_tp_sl_respects_available_to_sell(tmp_path: Path) -> None:
    cfg = _make_cfg()
    strat = TpSlTestStrategy(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="c1")
    coord.orders.in_flight_by_token[tid] = Decimal("4")
    run_id = RunId(str(uuid4()))

    async def _run() -> None:
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            _wire_sink(coord, sink, run_id)
            _buy_ack(strat, coord)
            await _run_monitor_until_triggered(strat.tp_sl_state, coord)

    asyncio.run(_run())
    assert strat.tp_sl_state._monitoring.final_size == Decimal("6")


def test_tp_sl_waits_for_allocation_before_monitor(tmp_path: Path) -> None:
    cfg = _make_cfg()
    state = TpSlTestState(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("5.319"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        _wire_sink(coord, sink, run_id)
        state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id="corr-live",
            entry_price=Decimal("0.5"),
            entry_price_source="buy_intent_limit",
            execution_mode=ExecutionMode.LIVE,
            apply_shadow_fill=False,
            match_evidence={},
        )
        state.try_promote_inventory(coord)
        assert state._monitoring is None
        assert len(state._pending_inventory) == 1
        coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("5.3099"), correlation_id="fill")
        coord.wallet.positions[tid] = WalletPosition(
            token_id=tid,
            qty=Decimal("5.3099"),
            avg_price_usd=Decimal("0.5"),
        )
        state.try_promote_inventory(coord)
    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines()]
    assert _health_events(rows, "tp_sl_waiting_for_allocation")
    assert _health_events(rows, "tp_sl_position_active")
    assert state._monitoring is not None
    assert state._monitoring.intended_buy_size == Decimal("5.319")
    assert state._monitoring.protected_qty == Decimal("5.3099")
    assert len(state._pending_inventory) == 0


def test_tp_sl_arms_on_partial_fill_not_intended_buy_size(tmp_path: Path) -> None:
    cfg = _make_cfg()
    state = TpSlTestState(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    intended = Decimal("5.319")
    actual = Decimal("5.3099")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=intended,
        limit_price=Decimal("0.94"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, actual, correlation_id="fill")
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid,
        qty=actual,
        avg_price_usd=Decimal("0.94"),
    )
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        _wire_sink(coord, sink, run_id)
        state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id="corr-partial",
            entry_price=Decimal("0.94"),
            entry_price_source="buy_intent_limit",
            execution_mode=ExecutionMode.LIVE,
            apply_shadow_fill=False,
            match_evidence={},
        )
    assert state._monitoring is not None
    assert state._monitoring.protected_qty == actual
    rows = [json.loads(line) for line in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert _health_events(rows, "tp_sl_position_active")
    reg = _health_events(rows, "tp_sl_registered")[0]["payload"]
    assert reg["lifecycle_phase"] == "position_active"
    assert reg["intended_buy_size"] == str(intended)
    assert reg["actual_allocated_qty"] == str(actual)
    assert reg["protected_qty"] == str(actual)
    assert reg["entry_price"] == "0.94"
    assert reg["entry_price_source"] == "avg_fill_price"


def test_tp_sl_exit_uses_pricing_at_trigger_time(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg_exit = TpSlTestExitConfig(
        enabled=True,
        size_mode="full_allocated_position",
        percent=Decimal("1"),
        fixed_size=None,
        order_style=OrderStyle.GTC,
        pricing_mode="auto",
        aggression_ticks=1,
        min_price=None,
        limit_price=Decimal("0.40"),
    )
    cfg = TpSlTestStrategyConfig(
        enabled=cfg.enabled,
        token_id=cfg.token_id,
        owner_id=cfg.owner_id,
        buy=cfg.buy,
        monitor=cfg.monitor,
        exit=cfg_exit,
        timeouts=cfg.timeouts,
        run_once=True,
    )
    strat = TpSlTestStrategy(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="c1")
    run_id = RunId(str(uuid4()))
    work: list = []

    async def _run() -> None:
        nonlocal work
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            _wire_sink(coord, sink, run_id)
            _buy_ack(strat, coord)
            await _run_monitor_until_triggered(strat.tp_sl_state, coord)
            resolved = ResolvedPrice(
                price=Decimal("0.58"),
                source="auto_book",
                best_ask=None,
                best_bid=Decimal("0.59"),
                tick_size=Decimal("0.01"),
                aggression_ticks=1,
                error=None,
            )
            with patch(
                "tyrex_pm.strategies.tp_sl_test.strategy.resolve_marketable_price_via_client",
                new=AsyncMock(return_value=resolved),
            ):
                work = await strat.tp_sl_state.resolve_triggered_work_units(
                    coord=coord, live_clob_client=object()
                )

    asyncio.run(_run())
    assert len(work) == 1
    assert work[0].intent.limit_price == Decimal("0.58")
    assert work[0].intent_fact_extensions.get("tp_sl_exit_pricing") is not None


def test_tp_sl_delayed_buy_allocation_without_wallet_does_not_monitor(tmp_path: Path) -> None:
    """Hong Kong live replay: allocation on submit, wallet still empty."""
    cfg = _make_cfg(
        fixture_prices=["0.61"],
        take_profit=None,
        stop_loss=None,
        take_profit_pct="0.05",
        stop_loss_pct="0.03",
        trigger_reference="entry_price",
    )
    state = TpSlTestState(cfg)
    strat = TpSlTestStrategy(cfg)
    strat._buy_submit_succeeded = True
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    intended = Decimal("7.462686567164179104477611940")
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, intended, correlation_id="buy")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=intended,
        limit_price=Decimal("0.67"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
        _wire_sink(coord, sink, run_id)
        state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id="corr-hk",
            entry_price=Decimal("0.67"),
            entry_price_source="auto_book",
            execution_mode=ExecutionMode.LIVE,
            apply_shadow_fill=False,
            match_evidence={"match_status": "delayed"},
        )
        assert state._monitoring is None
        assert len(state._pending_inventory) == 1
        asyncio.run(state.tick_monitor(coord, live_clob_client=None))
    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines()]
    assert _health_events(rows, "tp_sl_entry_pending")
    assert _health_events(rows, "tp_sl_waiting_for_position_active")
    assert not _health_events(rows, "tp_sl_monitor_tick")
    assert not _health_events(rows, "tp_sl_triggered")
    assert not strat.is_done()


def test_tp_sl_monitor_starts_only_when_protected_qty_positive(tmp_path: Path) -> None:
    cfg = _make_cfg()
    state = TpSlTestState(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        _wire_sink(coord, sink, run_id)
        state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id="c1",
            entry_price=Decimal("0.5"),
            entry_price_source="buy_limit_price",
            execution_mode=ExecutionMode.LIVE,
            apply_shadow_fill=False,
        )
        assert state._monitoring is None
        coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="fill")
        state.try_promote_inventory(coord)
        assert state._monitoring is None
        coord.wallet.positions[tid] = WalletPosition(
            token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
        )
        state.try_promote_inventory(coord)
    assert state._monitoring is not None
    assert state._monitoring.protected_qty == Decimal("10")


def test_tp_sl_pre_active_stop_loss_does_not_terminal_exit(tmp_path: Path) -> None:
    cfg = _make_cfg(fixture_prices=["0.39"])
    state = TpSlTestState(cfg)
    strat = TpSlTestStrategy(cfg)
    strat._buy_submit_succeeded = True
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="fill")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        _wire_sink(coord, sink, run_id)
        state.register_after_successful_buy(
            ap,
            coord,
            parent_correlation_id="c1",
            entry_price=Decimal("0.5"),
            entry_price_source="buy_limit_price",
            execution_mode=ExecutionMode.LIVE,
            apply_shadow_fill=False,
        )
        asyncio.run(state.tick_monitor(coord, live_clob_client=None))
    assert state._monitoring is None
    assert not state.is_terminal
    assert not strat.is_done()
    assert not _health_events(
        [json.loads(line) for line in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()],
        "tp_sl_triggered",
    )


def test_tp_sl_zero_final_size_does_not_mark_done(tmp_path: Path) -> None:
    cfg = _make_cfg(fixture_prices=["0.39"])
    state = TpSlTestState(cfg)
    strat = TpSlTestStrategy(cfg)
    strat._buy_submit_succeeded = True
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="fill")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))
    facts_path = tmp_path / "facts.jsonl"

    async def _run() -> list:
        with JsonlSink(facts_path) as sink:
            _wire_sink(coord, sink, run_id)
            state.register_after_successful_buy(
                ap,
                coord,
                parent_correlation_id="c1",
                entry_price=Decimal("0.5"),
                entry_price_source="avg_fill_price",
                execution_mode=ExecutionMode.LIVE,
                apply_shadow_fill=False,
            )
            await state.tick_monitor(coord, live_clob_client=None)
            coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("0"), avg_price_usd=Decimal("0.5"))
            return await state.resolve_triggered_work_units(coord=coord, live_clob_client=None)

    work = asyncio.run(_run())
    assert work == []
    assert not state.is_terminal
    assert not strat.is_done()
    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines()]
    assert _health_events(rows, "tp_sl_trigger_blocked_no_sellable_inventory")


def test_tp_sl_triggers_after_position_becomes_active(tmp_path: Path) -> None:
    cfg = _make_cfg(fixture_prices=["0.50", "0.39"])
    state = TpSlTestState(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="fill")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))

    async def _run() -> list:
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            _wire_sink(coord, sink, run_id)
            state.register_after_successful_buy(
                ap,
                coord,
                parent_correlation_id="c1",
                entry_price=Decimal("0.5"),
                entry_price_source="buy_limit_price",
                execution_mode=ExecutionMode.LIVE,
                apply_shadow_fill=False,
            )
            coord.wallet.positions[tid] = WalletPosition(
                token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
            )
            state.try_promote_inventory(coord)
            await state.tick_monitor(coord, live_clob_client=None)
            await state.tick_monitor(coord, live_clob_client=None)
            return await state.resolve_triggered_work_units(coord=coord, live_clob_client=None)

    work = asyncio.run(_run())
    assert len(work) == 1
    assert work[0].intent.size == Decimal("10")


def test_tp_sl_trigger_recomputes_final_size_from_current_inventory(tmp_path: Path) -> None:
    cfg = _make_cfg(fixture_prices=["0.50", "0.39"])
    state = TpSlTestState(cfg)
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId(cfg.token_id)
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    coord.allocation_ledger.apply_buy("tp_sl_test", tid, Decimal("10"), correlation_id="fill")
    coord.orders.in_flight_by_token[tid] = Decimal("4")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid"), run_id=RunId("r"))
    run_id = RunId(str(uuid4()))

    async def _run() -> list:
        with JsonlSink(tmp_path / "facts.jsonl") as sink:
            _wire_sink(coord, sink, run_id)
            state.register_after_successful_buy(
                ap,
                coord,
                parent_correlation_id="c1",
                entry_price=Decimal("0.5"),
                entry_price_source="avg_fill_price",
                execution_mode=ExecutionMode.LIVE,
                apply_shadow_fill=False,
            )
            await state.tick_monitor(coord, live_clob_client=None)
            await state.tick_monitor(coord, live_clob_client=None)
            return await state.resolve_triggered_work_units(coord=coord, live_clob_client=None)

    work = asyncio.run(_run())
    assert len(work) == 1
    assert work[0].intent.size == Decimal("6")


def test_resolve_entry_price_prefers_avg_fill(tmp_path: Path) -> None:
    coord = _coord_with_ledger(tmp_path)
    tid = TokenId("tok")
    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("5"), avg_price_usd=Decimal("0.63"))
    price, source = resolve_entry_price(
        coord,
        tid,
        limit_price=Decimal("0.67"),
        match_evidence={"price": "0.65"},
    )
    assert price == Decimal("0.63")
    assert source == "avg_fill_price"


def test_tp_sl_run_once_terminal_semantics(tmp_path: Path) -> None:
    rows = asyncio.run(_trigger_exit(tmp_path, ["0.50", "0.61"], "take_profit"))
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_tp_sl_strategy_dict(fixture_prices=["0.50", "0.61"]),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.tp_sl_test is not None
    strat = TpSlTestStrategy(app.tp_sl_test)
    strat._buy_submit_succeeded = True
    strat.tp_sl_state.mark_exit_terminal("sell_submitted")
    assert strat.is_done()
    assert _health_events(rows, "tp_sl_exit_intent_emitted")
