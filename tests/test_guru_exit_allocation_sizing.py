"""P5: ledger-aware guru mirror SELL sizing tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import ExitIntent, GuruTradeSignal, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_ALLOCATION_LEDGER,
    FACT_TYPE_HEALTH,
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_STRATEGY_SKIP,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_ids import OWNER_GURU_FOLLOW, OWNER_SELL_TEST
from tyrex_pm.runtime.config import load_app_config, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_intent_work_unit, process_new_guru_signals
from tyrex_pm.signals.guru_copy_signal import to_copy_signal
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import StrategyStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient

TOKEN = TokenId("1234567890")


def _guru_sell_signal(*, size: str = "100", dedup_key: str = "sell-1") -> GuruTradeSignal:
    return GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TOKEN,
        side=Side.SELL,
        size=Decimal(size),
        price=Decimal("0.5"),
        notional_usd=Decimal("50"),
        dedup_key=dedup_key,
        ts_venue=datetime.now(timezone.utc),
    )


def _guru_buy_signal(*, size: str = "10", dedup_key: str = "buy-1") -> GuruTradeSignal:
    return GuruTradeSignal(
        guru_wallet="0xg",
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal(size),
        price=Decimal("0.5"),
        notional_usd=Decimal("5"),
        dedup_key=dedup_key,
        ts_venue=datetime.now(timezone.utc),
    )


def _wire_coord(
    tmp_path: Path,
    *,
    wallet_qty: Decimal = Decimal("0"),
    in_flight: Decimal = Decimal("0"),
) -> tuple[RuntimeCoordinator, JsonlSink, str, AllocationLedger]:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    if wallet_qty > 0:
        coord.wallet.positions[TOKEN] = WalletPosition(
            token_id=TOKEN,
            qty=wallet_qty,
            avg_price_usd=Decimal("0.5"),
        )
    if in_flight > 0:
        coord.orders.in_flight_by_token[TOKEN] = in_flight
    run_id = str(uuid4())
    sink = JsonlSink(tmp_path / "facts.jsonl")
    sink.__enter__()
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    coord.allocation_ledger = ledger
    coord.allocation_ledger_run_id = run_id
    coord.allocation_ledger_sink = sink
    return coord, sink, run_id, ledger


def _guru_app(*, sell_mode: str = "proportional_to_guru") -> object:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root)
    strat = dict(app.raw["strategy"])
    strat["exits"] = dict(strat.get("exits", {}))
    strat["exits"]["sell_mode"] = sell_mode
    runtime = dict(app.raw["runtime"])
    runtime["allocation_ledger"] = {}
    return parse_app_config(risk=app.raw["risk"], strategy=strat, runtime=runtime)


def _facts(tmp_path: Path) -> list[dict]:
    return [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]


def test_guru_exit_always_uses_allocation(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, _run_id, ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    ledger.apply_buy(OWNER_GURU_FOLLOW, TOKEN, Decimal("4"), correlation_id="seed")

    intents, skip, meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal(size="100")), coord)
    assert skip is None
    assert len(intents) == 1
    assert intents[0].size == Decimal("4")
    assert meta is not None
    assert meta["guru_exit_sizing"]["wallet_position_qty"] == "10"
    assert meta["guru_exit_sizing"]["allocated_available"] == "4"
    sink.__exit__(None, None, None)


def test_guru_exit_no_wallet_only_fallback(tmp_path: Path) -> None:
    """Wallet qty alone must not produce a SELL when guru_follow allocation is zero."""
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, _run_id, _ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))

    intents, skip, meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal()), coord)
    assert intents == []
    assert skip == rc.GURU_NO_ALLOCATED_INVENTORY
    assert meta is not None
    assert meta["guru_exit_health"]["event"] == "guru_exit_allocation_blocked"
    sink.__exit__(None, None, None)


def test_full_bot_position_uses_allocated_position_only(tmp_path: Path) -> None:
    app = _guru_app(sell_mode="full_bot_position")
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, _run_id, ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    ledger.apply_buy(OWNER_GURU_FOLLOW, TOKEN, Decimal("3"), correlation_id="seed")

    intents, skip, _meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal(size="1")), coord)
    assert skip is None
    assert len(intents) == 1
    assert intents[0].size == Decimal("3")
    sink.__exit__(None, None, None)


def test_guru_exit_wallet_qty_but_zero_allocation_blocked(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, _run_id, _ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))

    intents, skip, meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal()), coord)
    assert intents == []
    assert skip == rc.GURU_NO_ALLOCATED_INVENTORY
    assert meta is not None
    assert meta["guru_exit_health"]["event"] == "guru_exit_allocation_blocked"
    assert meta["guru_exit_health"]["allocated_available"] == "0"
    assert meta["guru_exit_health"]["wallet_position_qty"] == "10"
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_guru_exit_wallet_qty_but_zero_allocation_blocked_pipeline(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, run_id, _ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    await process_new_guru_signals(
        [_guru_sell_signal()],
        app=app,
        run_id=RunId(run_id),
        strategy=gf,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
    )
    rows = _facts(tmp_path)
    skips = [r for r in rows if r["fact_type"] == FACT_TYPE_STRATEGY_SKIP]
    assert len(skips) == 1
    assert skips[0]["payload"]["reason"] == rc.GURU_NO_ALLOCATED_INVENTORY
    health = [r for r in rows if r["fact_type"] == FACT_TYPE_HEALTH]
    assert len(health) == 1
    assert health[0]["payload"]["event"] == "guru_exit_allocation_blocked"
    assert not [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    sink.__exit__(None, None, None)


def test_guru_exit_clamped_to_allocation(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, run_id, ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    ledger.apply_buy(OWNER_GURU_FOLLOW, TOKEN, Decimal("3"), correlation_id="seed")

    intents, skip, meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal()), coord)
    assert skip is None
    assert len(intents) == 1
    assert isinstance(intents[0], ExitIntent)
    assert intents[0].size == Decimal("3")
    assert meta is not None
    assert meta["guru_exit_health"]["event"] == "guru_exit_allocation_clamped"
    assert meta["guru_exit_sizing"]["final_size"] == "3"
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_guru_exit_clamped_to_allocation_pipeline_intent_created(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, run_id, ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    ledger.apply_buy(OWNER_GURU_FOLLOW, TOKEN, Decimal("3"), correlation_id="seed")
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    await process_new_guru_signals(
        [_guru_sell_signal(dedup_key="clamp-1")],
        app=app,
        run_id=RunId(run_id),
        strategy=gf,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
    )
    rows = _facts(tmp_path)
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT]
    assert len(intents) == 1
    assert intents[0]["payload"]["size"] == "3"
    assert "guru_exit_sizing" in intents[0]["payload"]
    assert intents[0]["payload"]["guru_exit_sizing"]["final_size"] == "3"
    clamped = [r for r in rows if r["fact_type"] == FACT_TYPE_HEALTH and r["payload"].get("event") == "guru_exit_allocation_clamped"]
    assert len(clamped) == 1
    sink.__exit__(None, None, None)


def test_guru_exit_respects_venue_available_to_sell(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, _run_id, ledger = _wire_coord(
        tmp_path,
        wallet_qty=Decimal("10"),
        in_flight=Decimal("8"),
    )
    ledger.apply_buy(OWNER_GURU_FOLLOW, TOKEN, Decimal("10"), correlation_id="seed")

    intents, skip, meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal()), coord)
    assert skip is None
    assert len(intents) == 1
    assert intents[0].size <= Decimal("2")
    assert meta is not None
    assert meta["guru_exit_sizing"]["available_to_sell"] == "2"
    sink.__exit__(None, None, None)


def test_guru_exit_full_bot_position_mode_uses_allocation(tmp_path: Path) -> None:
    app = _guru_app(sell_mode="full_bot_position")
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, _run_id, ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    ledger.apply_buy(OWNER_GURU_FOLLOW, TOKEN, Decimal("3"), correlation_id="seed")

    intents, skip, meta = gf.on_guru_signal(to_copy_signal(_guru_sell_signal(size="1")), coord)
    assert skip is None
    assert len(intents) == 1
    assert intents[0].size == Decimal("3")
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_guru_buy_then_sell_allocation_round_trip(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    await process_new_guru_signals(
        [_guru_buy_signal(dedup_key="rt-buy")],
        app=app,
        run_id=RunId(run_id),
        strategy=gf,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
    )
    assert ledger.get_allocated(OWNER_GURU_FOLLOW, TOKEN) > 0
    allocated_after_buy = ledger.get_allocated(OWNER_GURU_FOLLOW, TOKEN)

    await process_new_guru_signals(
        [_guru_sell_signal(size=str(allocated_after_buy), dedup_key="rt-sell")],
        app=app,
        run_id=RunId(run_id),
        strategy=gf,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
    )
    assert ledger.get_allocated(OWNER_GURU_FOLLOW, TOKEN) == Decimal("0")
    assert ledger.get_reserved(OWNER_GURU_FOLLOW, TOKEN) == Decimal("0")
    events = [r["payload"]["event"] for r in _facts(tmp_path) if r["fact_type"] == FACT_TYPE_ALLOCATION_LEDGER]
    assert "allocation_buy_applied" in events
    assert "allocation_sell_applied" in events
    oms = [r for r in _facts(tmp_path) if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    assert len(oms) == 2
    sink.__exit__(None, None, None)


@pytest.mark.asyncio
async def test_guru_sell_with_foreign_allocation_blocked(tmp_path: Path) -> None:
    app = _guru_app()
    gf = GuruFollowStrategy(app.strategy)
    coord, sink, run_id, ledger = _wire_coord(tmp_path, wallet_qty=Decimal("10"))
    ledger.apply_buy(OWNER_SELL_TEST, TOKEN, Decimal("10"), correlation_id="foreign")
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    await process_new_guru_signals(
        [_guru_sell_signal(dedup_key="foreign-block")],
        app=app,
        run_id=RunId(run_id),
        strategy=gf,
        coord=coord,
        sink=sink,
        oms=ShadowOMS(),
    )
    rows = _facts(tmp_path)
    assert ledger.get_allocated(OWNER_GURU_FOLLOW, TOKEN) == Decimal("0")
    skips = [r for r in rows if r["fact_type"] == FACT_TYPE_STRATEGY_SKIP]
    assert len(skips) == 1
    assert skips[0]["payload"]["reason"] == rc.GURU_NO_ALLOCATED_INVENTORY
    assert not [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    sink.__exit__(None, None, None)
