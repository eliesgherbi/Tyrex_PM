"""Live2 regression: emergency unwind below min_notional must pass risk."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.enums import ExecutionMode, OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import ExitIntent, WalletPosition
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_REDUCE_ONLY_MIN_NOTIONAL_BYPASS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.risk.engine import evaluate_intent
from tyrex_pm.runtime.config import parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.intent_work import IntentWorkUnit
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy
from tyrex_pm.strategies.paired_binary.sizing import ExitSizing, build_exit_work_unit

YES = TokenId("9059650700126795019827485089957938050581213031053374092199507389736394347163")


def _app():
    return parse_app_config(
        risk={
            "notional": {"min_usd": "1", "max_usd": "15", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "20", "portfolio_cap_usd": "30"},
            "capital": {"enabled": False},
            "inventory": {"sell_requires_venue_position": True},
            "venue_min_size": {"enabled": False},
            "concurrency": {"max_orders_in_flight": 10},
            "readiness": {"require_wallet_sync": False},
            "exits": {"allow_reduce_only_mark_fallback": True},
        },
        strategy={
            "kind": "paired_binary",
            "enabled": True,
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": str(YES),
                "no_token_id": "n1",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
            "execution": {"planner": {"enabled": True}},
            "market_data": {"enabled": True, "max_book_age_s": 5},
        },
    )


def _ctx():
    qty = Decimal("1.79")
    return (
        RunId("live2-regression"),
        {
            "execution_mode": ExecutionMode.SHADOW,
            "wallet_positions": (
                WalletPosition(token_id=YES, qty=qty, avg_price_usd=Decimal("0.54")),
            ),
            "open_orders": (),
            "usdc_balance": Decimal("100"),
            "usdc_allowance": Decimal("100"),
            "last_wallet_sync_ts": datetime.now(timezone.utc),
            "mark_prices": {YES: Decimal("0.42")},
            "kill_switch": False,
            "health_ok": True,
            "heartbeat_ok": True,
            "clob_session_ok": True,
            "in_flight_order_count": 0,
            "orders_in_flight_by_token": {},
            "reconcile_drift": False,
            "venue_truth_stale": False,
        },
    )


def test_live2_regression_risk_approves_emergency_unwind_below_min_notional() -> None:
    app = _app()
    run_id, ctx_kwargs = _ctx()
    from tyrex_pm.core.models import RiskContext

    intent = ExitIntent(
        token_id=YES,
        side=Side.SELL,
        size=Decimal("1.79"),
        limit_price=Decimal("0.42"),
        order_style=OrderStyle.FAK,
        urgency="urgent",
    )
    ext = {
        "paired_binary_reason": "entry_fill_timeout",
        "paired_binary_sizing": {
            "owner_allocation": "1.79",
            "venue_available": "1.79",
            "final_size": "1.79",
        },
    }
    decision = evaluate_intent(
        intent,
        RiskContext(**ctx_kwargs),
        app=app,
        run_id=run_id,
        reduce_only_context="entry_fill_timeout",
        intent_extensions=ext,
    )
    assert decision.approved, decision.reason_codes
    assert "notional_below_min" not in decision.reason_codes
    assert decision.extensions is not None
    assert decision.extensions.get("reduce_only_min_notional_bypass") is True
    assert decision.extensions.get("reduce_only_bypass_fact") is not None


async def _run_pipeline_emits_bypass_fact(tmp_path: Path) -> None:
    app = _app()
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    coord.allocation_ledger.apply_buy("paired_binary", YES, Decimal("1.79"), correlation_id="c1")
    coord.wallet.positions[YES] = WalletPosition(
        token_id=YES, qty=Decimal("1.79"), avg_price_usd=Decimal("0.54")
    )
    from tyrex_pm.state.market_store import MarketStateStore, make_snapshot

    store = MarketStateStore(default_max_age_s=5.0)
    store.apply_snapshot(
        make_snapshot(YES, bids=[(Decimal("0.42"), Decimal("100"))], asks=[(Decimal("0.45"), Decimal("100"))])
    )
    coord.market_state = store

    cfg = app.paired_binary
    assert cfg is not None
    strategy = PairedBinaryStrategy(cfg)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        oms = ShadowOMS()
        sizing = ExitSizing(
            planned_before_clamp=Decimal("1.79"),
            owner_allocation=Decimal("1.79"),
            venue_available=Decimal("1.79"),
            final_size=Decimal("1.79"),
        )
        work = build_exit_work_unit(
            token_id=YES,
            size=Decimal("1.79"),
            limit_price=Decimal("0.42"),
            order_style=OrderStyle.FAK,
            owner_id="paired_binary",
            pair_correlation_id="pc1",
            leg="yes",
            leg_correlation_id="pc1:yes:unwind",
            reason="entry_fill_timeout",
            sizing=sizing,
        )
        assert work is not None
        await process_intent_work_unit(
            work,
            app=app,
            run_id=RunId("live2-pipeline"),
            strategy=strategy,
            coord=coord,
            sink=sink,
            oms=oms,
        )
        lines = sink._path.read_text(encoding="utf-8").strip().splitlines()
    facts = [json.loads(ln) for ln in lines if ln.strip()]
    bypass = [f for f in facts if f.get("fact_type") == FACT_TYPE_REDUCE_ONLY_MIN_NOTIONAL_BYPASS]
    assert bypass, "expected reduce_only_min_notional_bypass fact"
    assert bypass[0]["payload"]["context"] == "entry_fill_timeout"
    risk_facts = [f for f in facts if f.get("fact_type") == "risk_decision" and f["payload"].get("approved")]
    assert risk_facts


def test_live2_pipeline_emits_bypass_fact(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_run_pipeline_emits_bypass_fact(tmp_path))
