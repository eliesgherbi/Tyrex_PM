from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from py_clob_client.exceptions import PolyApiException

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import EnterIntent, RiskContext
from tyrex_pm.ingestion.guru_stream import process_fixture_signals
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.runtime.config import load_app_config, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_new_guru_signals
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.strategy_store import StrategyStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.guru_follow.strategy import GuruFollowStrategy
from tyrex_pm.venue.polymarket.data_api_client import DataApiClient


def _ctx(**over) -> RiskContext:
    base = dict(
        execution_mode=ExecutionMode.SHADOW,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=Decimal("1000000"),
        usdc_allowance=Decimal("1000000"),
        last_wallet_sync_ts=datetime.now(timezone.utc),
        mark_prices={TokenId("1234567890"): Decimal("0.5")},
        kill_switch=False,
        health_ok=True,
        heartbeat_ok=True,
        clob_session_ok=True,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
        venue_truth_stale=False,
    )
    base.update(over)
    return RiskContext(**base)


def test_notional_cap_clips_buy_intent() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    app2 = parse_app_config(
        risk={
            **app.raw["risk"],
            "notional": {"min_usd": "1", "max_usd": "10", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "100000", "portfolio_cap_usd": "1000000"},
        },
        strategy=app.raw["strategy"],
        runtime=app.raw["runtime"],
    )
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("100"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    d = evaluate_intent(intent, _ctx(), app=app2, run_id=RunId("r"))
    assert d.approved and d.approved_intent is not None
    assert d.approved_intent.intent.size == Decimal("20")
    assert d.extensions is not None
    assert d.extensions.get("notional_capped") is True
    assert d.extensions.get("notional_max_policy") == "cap"


def test_notional_deny_rejects_above_max() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    app2 = parse_app_config(
        risk={
            **app.raw["risk"],
            "notional": {"min_usd": "1", "max_usd": "10", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "100000", "portfolio_cap_usd": "1000000"},
        },
        strategy=app.raw["strategy"],
        runtime=app.raw["runtime"],
    )
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("100"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    d = evaluate_intent(intent, _ctx(), app=app2, run_id=RunId("r"))
    assert not d.approved
    assert rc.NOTIONAL_ABOVE_MAX in d.reason_codes
    assert d.extensions is not None
    assert d.extensions.get("notional_denied_above_max") is True


def test_capital_gate_denies_insufficient_balance() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    app2 = parse_app_config(
        risk={
            **app.raw["risk"],
            "notional": {"min_usd": "1", "max_usd": "1000", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "100000", "portfolio_cap_usd": "1000000"},
            "capital": {"enabled": True, "max_wallet_age_s": 120},
        },
        strategy=app.raw["strategy"],
        runtime=app.raw["runtime"],
    )
    intent = EnterIntent(
        token_id=TokenId("1234567890"),
        side=Side.BUY,
        size=Decimal("100"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    d = evaluate_intent(
        intent,
        _ctx(usdc_balance=Decimal("10"), usdc_allowance=Decimal("1000000")),
        app=app2,
        run_id=RunId("r"),
    )
    assert not d.approved
    assert rc.INSUFFICIENT_CAPITAL in d.reason_codes
    assert d.extensions is not None
    assert d.extensions.get("capital_gate_checked") is True


def test_capital_enabled_when_capital_section_omitted() -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    risk = {k: v for k, v in app.raw["risk"].items() if k != "capital"}
    app2 = parse_app_config(risk=risk, strategy=app.raw["strategy"], runtime=app.raw["runtime"])
    assert app2.risk.capital.enabled is True


class _BoomOMS:
    async def submit(self, ap) -> str:
        raise PolyApiException(httpx.Response(400, json={"error": "insufficient balance"}))

    async def cancel(self, ac) -> str:
        return "{}"


@pytest.mark.asyncio
async def test_oms_reject_poly_does_not_crash_writes_fact(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    app = load_app_config(repo_root=root, scenario_file="shadow_guru")
    app2 = parse_app_config(
        risk={
            **app.raw["risk"],
            "notional": {"min_usd": "0.01", "max_usd": "500", "max_policy": "deny"},
            "deployment": {"token_cap_usd": "100000", "portfolio_cap_usd": "1000000"},
        },
        strategy=app.raw["strategy"],
        runtime=app.raw["runtime"],
    )
    assert app.runtime.shadow_bootstrap is not None
    run_id = RunId(str(uuid4()))
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    apply_shadow_bootstrap(coord.wallet, app.runtime.shadow_bootstrap)

    fixture = root / "tests" / "fixtures" / "data_api" / "activity_batch.json"
    sigs = DataApiClient.parse_activity_json(fixture.read_text(encoding="utf-8"), "0xguru")
    store = StrategyStore()
    new = process_fixture_signals(sigs, store)
    new = [s for s in new if s.side == Side.BUY][:1]
    assert new

    facts_path = tmp_path / "facts.jsonl"
    strat = GuruFollowStrategy(app2.strategy)
    with JsonlSink(facts_path) as sink:
        await process_new_guru_signals(
            new,
            app=app2,
            run_id=run_id,
            strategy=strat,
            coord=coord,
            sink=sink,
            oms=_BoomOMS(),
        )

    import json

    rows = [json.loads(line) for line in facts_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rejects = [r for r in rows if r["fact_type"] == "oms_reject"]
    assert rejects, "expected oms_reject fact"
    assert rejects[0]["payload"].get("status_code") == 400
    assert coord.orders.in_flight_order_count == 0
