"""Tests for the standalone sell_test strategy.

Covers:

* Config parsing (``kind: sell_test`` YAML produces a ``SellTestStrategyConfig``).
* ``initial_buy_work_units()`` emits one BUY then nothing more (run_once).
* Shadow + instant fill: SELL arms immediately and drains via the pipeline.
* Live: SELL stays pending until ``available_to_sell >= planned_sell_size``.
* End-to-end shadow: BUY → demo loop → SELL fact with sell_test provenance.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import ApprovedIntent, EnterIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_INTENT,
    FACT_TYPE_OMS_SUBMIT,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import (
    SellTestBuyConfig,
    SellTestSellConfig,
    SellTestStrategyConfig,
    parse_app_config,
)
from tyrex_pm.runtime.allocation_ids import OWNER_SELL_TEST
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import (
    process_intent_work_unit,
    process_scheduled_exit_demo_due,
)
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.sell_test.strategy import (
    SELL_TEST_FACT_SOURCE,
    SellTestState,
    SellTestStrategy,
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


def _sell_test_strategy_dict(token_id: str = "tok-test", delay_s: float = 0.0) -> dict:
    return {
        "kind": "sell_test",
        "enabled": True,
        "token_id": token_id,
        "buy": {
            "enabled": True,
            "notional_usd": "5",
            "limit_price": "0.50",
            "order_style": "GTC",
        },
        "sell": {
            "enabled": True,
            "delay_s": delay_s,
            "order_style": "GTC",
        },
        "run_once": True,
    }


def test_parse_sell_test_strategy_yaml() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_sell_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.sell_test is not None
    assert app.sell_test.token_id == "tok-test"
    assert app.sell_test.buy.notional_usd == Decimal("5")
    assert app.sell_test.buy.limit_price == Decimal("0.50")
    assert app.sell_test.buy.order_style == OrderStyle.GTC
    assert app.sell_test.sell.order_style == OrderStyle.GTC
    assert app.sell_test.sell.limit_price is None
    assert app.sell_test.run_once is True


def test_parse_sell_test_strategy_requires_token_id() -> None:
    bad = _sell_test_strategy_dict()
    bad["token_id"] = ""
    with pytest.raises(ConfigError):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_sell_test_strategy_requires_buy_limit_price_when_enabled() -> None:
    bad = _sell_test_strategy_dict()
    bad["buy"].pop("limit_price")
    with pytest.raises(ConfigError):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_initial_buy_work_units_run_once() -> None:
    cfg = SellTestStrategyConfig(
        enabled=True,
        token_id="tok-1",
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.5"),
            order_style=OrderStyle.GTC,
        ),
        sell=SellTestSellConfig(
            enabled=True, delay_s=0.0, order_style=OrderStyle.GTC, limit_price=None
        ),
        run_once=True,
    )
    strat = SellTestStrategy(cfg)
    first = strat.initial_buy_work_units()
    assert len(first) == 1
    assert isinstance(first[0].intent, EnterIntent)
    assert first[0].intent.side == Side.BUY
    assert first[0].intent.size == Decimal("10")  # 5 / 0.5
    assert first[0].intent.limit_price == Decimal("0.5")
    second = strat.initial_buy_work_units()
    assert second == []


def test_live_pending_arms_when_inventory_sufficient() -> None:
    cfg = SellTestStrategyConfig(
        enabled=True,
        token_id="tok-live",
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.5"),
            order_style=OrderStyle.GTC,
        ),
        sell=SellTestSellConfig(
            enabled=True, delay_s=3.0, order_style=OrderStyle.GTC, limit_price=None
        ),
        run_once=True,
    )
    state = SellTestState(cfg)
    tid = TokenId("tok-live")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(intent=ent, client_order_id=ClientOrderId("cid-live"), run_id=RunId("r-live"))
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    ledger = AllocationLedger()
    coord.allocation_ledger = ledger
    ledger.apply_buy(OWNER_SELL_TEST, tid, Decimal("10"), correlation_id="corr-live")
    state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-live",
        execution_mode=ExecutionMode.LIVE,
        apply_shadow_fill=False,
    )
    assert len(state._pending_live) == 1
    assert state._armed == []

    state.try_arm_live_pending(coord)
    assert len(state._pending_live) == 1, "no inventory yet -> stays pending"

    coord.wallet.positions[tid] = WalletPosition(token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    state.try_arm_live_pending(coord)
    assert state._pending_live == []
    assert len(state._armed) == 1


def _shadow_app() -> tuple:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_sell_test_strategy_dict(token_id="tok-e2e", delay_s=0.0),
        runtime=dict(_BASE_RUNTIME),
    )
    return app


async def _shadow_buy_then_drain_sell(tmp_path: Path) -> list[dict]:
    app = _shadow_app()
    assert app.sell_test is not None
    strat = SellTestStrategy(app.sell_test)
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / "sell_test_alloc.json")
    assert app.runtime.shadow_bootstrap is not None
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)
    run_id = RunId(str(uuid4()))
    facts_path = tmp_path / "facts.jsonl"
    with JsonlSink(facts_path) as sink:
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
        await asyncio.sleep(0.05)
        await process_scheduled_exit_demo_due(
            strategy=strat,
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            apply_local_shadow_fill=True,
            live_clob_client=None,
        )
    return [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_shadow_end_to_end_emits_buy_then_sell_with_provenance(tmp_path: Path) -> None:
    rows = asyncio.run(_shadow_buy_then_drain_sell(tmp_path))
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT]
    assert len(intents) == 2
    buy_row = next(r for r in intents if r["payload"]["side"] == "BUY")
    sell_row = next(r for r in intents if r["payload"]["side"] == "SELL")

    assert buy_row["payload"]["source"] == SELL_TEST_FACT_SOURCE
    assert buy_row["payload"]["sell_test_token_id"] == "tok-e2e"

    assert sell_row["payload"]["source"] == SELL_TEST_FACT_SOURCE
    assert sell_row["payload"]["parent_correlation_id"] == buy_row["correlation_id"]
    assert "parent_buy_intent_id" in sell_row["payload"]
    assert "parent_client_order_id" in sell_row["payload"]
    assert sell_row["payload"]["limit_price"] == "0.50"

    submits = [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    assert len(submits) == 2, "expected one shadow submit per intent (BUY + SELL)"


def test_strategy_is_done_after_full_cycle(tmp_path: Path) -> None:
    rows = asyncio.run(_shadow_buy_then_drain_sell(tmp_path))
    intents = [r for r in rows if r["fact_type"] == FACT_TYPE_INTENT]
    assert sum(1 for r in intents if r["payload"]["side"] == "SELL") == 1


# ---------------------- auto-pricing parser tests ------------------------------


def test_parse_sell_test_auto_pricing_allows_missing_buy_limit_price() -> None:
    cfg = _sell_test_strategy_dict()
    cfg["buy"]["pricing_mode"] = "auto"
    cfg["buy"].pop("limit_price")
    cfg["buy"]["aggression_ticks"] = 2
    cfg["buy"]["max_price"] = "0.95"
    cfg["sell"]["pricing_mode"] = "auto"
    cfg["sell"]["aggression_ticks"] = 3
    cfg["sell"]["min_price"] = "0.05"
    app = parse_app_config(risk=dict(_BASE_RISK), strategy=cfg, runtime=dict(_BASE_RUNTIME))
    assert app.sell_test is not None
    assert app.sell_test.buy.pricing_mode == "auto"
    assert app.sell_test.buy.limit_price is None
    assert app.sell_test.buy.aggression_ticks == 2
    assert app.sell_test.buy.max_price == Decimal("0.95")
    assert app.sell_test.sell.pricing_mode == "auto"
    assert app.sell_test.sell.aggression_ticks == 3
    assert app.sell_test.sell.min_price == Decimal("0.05")


def test_parse_sell_test_rejects_unknown_pricing_mode() -> None:
    bad = _sell_test_strategy_dict()
    bad["buy"]["pricing_mode"] = "banana"
    with pytest.raises(ConfigError):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_parse_sell_test_rejects_negative_aggression() -> None:
    bad = _sell_test_strategy_dict()
    bad["sell"]["aggression_ticks"] = -1
    with pytest.raises(ConfigError):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


# ---------------------- BUY auto-pricing override tests -----------------------


def test_set_resolved_buy_price_overrides_initial_buy() -> None:
    cfg = SellTestStrategyConfig(
        enabled=True,
        token_id="tok-auto",
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.50"),  # fallback
            order_style=OrderStyle.GTC,
            pricing_mode="auto",
            aggression_ticks=1,
            max_price=None,
        ),
        sell=SellTestSellConfig(
            enabled=True,
            delay_s=0.0,
            order_style=OrderStyle.GTC,
            limit_price=None,
            pricing_mode="fixed",
            aggression_ticks=1,
            min_price=None,
        ),
        run_once=True,
    )
    strat = SellTestStrategy(cfg)
    strat.set_resolved_buy_price(
        Decimal("0.56"),
        evidence={"source": "auto_book", "best_ask": "0.55"},
    )
    out = strat.initial_buy_work_units()
    assert len(out) == 1
    intent = out[0].intent
    assert isinstance(intent, EnterIntent)
    assert intent.limit_price == Decimal("0.56")
    # Size is now notional / resolved price = 5 / 0.56.
    assert intent.size == Decimal("5") / Decimal("0.56")
    ext = out[0].intent_fact_extensions
    assert ext is not None
    assert ext["sell_test_buy_pricing_mode"] == "auto"
    assert ext["sell_test_buy_pricing"]["best_ask"] == "0.55"


def test_set_resolved_buy_price_rejects_non_positive() -> None:
    cfg = SellTestStrategyConfig(
        enabled=True,
        token_id="tok-auto",
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.50"),
            order_style=OrderStyle.GTC,
            pricing_mode="auto",
            aggression_ticks=1,
            max_price=None,
        ),
        sell=SellTestSellConfig(
            enabled=True, delay_s=0.0, order_style=OrderStyle.GTC, limit_price=None
        ),
        run_once=True,
    )
    strat = SellTestStrategy(cfg)
    with pytest.raises(ValueError):
        strat.set_resolved_buy_price(Decimal("0"))


# ---------------------- SELL auto-pricing async resolver ----------------------


class _StubBookClient:
    """Minimal V2-SDK-shaped client returning a fixed book for one token."""

    def __init__(self, book: dict) -> None:
        self._book = book
        self.calls: list[str] = []

    def get_order_book(self, token_id: str) -> dict:
        self.calls.append(token_id)
        return self._book


async def _drive_one_sell_cycle(
    *, sell_pricing_mode: str, live_clob: object | None
) -> tuple[SellTestStrategy, list]:
    cfg = SellTestStrategyConfig(
        enabled=True,
        token_id="tok-auto-sell",
        buy=SellTestBuyConfig(
            enabled=True,
            notional_usd=Decimal("5"),
            limit_price=Decimal("0.50"),
            order_style=OrderStyle.GTC,
            pricing_mode="fixed",
            aggression_ticks=1,
            max_price=None,
        ),
        sell=SellTestSellConfig(
            enabled=True,
            delay_s=0.0,
            order_style=OrderStyle.GTC,
            limit_price=Decimal("0.49"),  # fallback
            pricing_mode=sell_pricing_mode,
            aggression_ticks=1,
            min_price=None,
        ),
        run_once=True,
    )
    strat = SellTestStrategy(cfg)
    tid = TokenId("tok-auto-sell")
    ent = EnterIntent(
        token_id=tid,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.50"),
        order_style=OrderStyle.GTC,
    )
    ap = ApprovedIntent(
        intent=ent, client_order_id=ClientOrderId("cid-auto"), run_id=RunId("r-auto")
    )
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    strat.sell_test_state.register_after_successful_buy(
        ap,
        coord,
        parent_correlation_id="corr-auto",
        execution_mode=ExecutionMode.SHADOW,
        apply_shadow_fill=True,
    )
    work_units = await strat.resolve_due_work_units(coord=coord, live_clob_client=live_clob)
    return strat, work_units


def test_resolve_due_work_units_auto_uses_book_price() -> None:
    book = {"bids": [{"price": "0.45", "size": "10"}], "asks": [{"price": "0.55", "size": "8"}]}
    client = _StubBookClient(book)
    strat, wus = asyncio.run(
        _drive_one_sell_cycle(sell_pricing_mode="auto", live_clob=client)
    )
    assert len(wus) == 1
    intent = wus[0].intent
    # best_bid - 1 tick (default 0.01) = 0.44
    assert intent.limit_price == Decimal("0.44")
    ev = wus[0].intent_fact_extensions
    assert ev is not None
    assert ev["sell_test_pricing"]["source"] == "auto_book"
    assert ev["sell_test_pricing"]["best_bid"] == "0.45"
    assert client.calls == ["tok-auto-sell"]


def test_resolve_due_work_units_fixed_mode_uses_config_price() -> None:
    client = _StubBookClient({"bids": [{"price": "0.45", "size": "10"}], "asks": []})
    strat, wus = asyncio.run(
        _drive_one_sell_cycle(sell_pricing_mode="fixed", live_clob=client)
    )
    assert len(wus) == 1
    # Fixed mode keeps the registered sell_limit_price (cfg.sell.limit_price=0.49).
    assert wus[0].intent.limit_price == Decimal("0.49")
    # Fixed mode should NOT call the book.
    assert client.calls == []
    ev = wus[0].intent_fact_extensions
    assert ev is not None
    assert "sell_test_pricing" not in ev


def test_resolve_due_work_units_auto_without_live_client_falls_back_to_fixed() -> None:
    strat, wus = asyncio.run(_drive_one_sell_cycle(sell_pricing_mode="auto", live_clob=None))
    assert len(wus) == 1
    # No client = no book lookup, behaves exactly like fixed mode.
    assert wus[0].intent.limit_price == Decimal("0.49")
