"""Phase 3 (architecture_enhance): ExecutionPlanner unit + integration tests."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import ClientOrderId, RunId, TokenId
from tyrex_pm.core.models import (
    ApprovedIntent,
    EnterIntent,
    ExitIntent,
    URGENCY_NORMAL,
    URGENCY_PASSIVE,
    URGENCY_URGENT,
)
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.execution.planner import ExecutionPlanner
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_EXECUTION_PLAN,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_RISK,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ExecutionPlannerConfig, ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_signals
from tyrex_pm.signals.simple_signal import SimpleSignal
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.simple_signal_test.strategy import SimpleSignalTestStrategy

TOKEN = TokenId("token-plan")


def _approved(intent) -> ApprovedIntent:
    return ApprovedIntent(intent=intent, client_order_id=ClientOrderId("cid-1"), run_id=RunId("r1"))


def _entry(urgency: str = URGENCY_NORMAL, limit: Decimal | None = Decimal("0.5"), *, style: OrderStyle = OrderStyle.GTC) -> EnterIntent:
    return EnterIntent(
        token_id=TOKEN,
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=limit,
        order_style=style,
        urgency=urgency,
    )


def _urgent_exit(size: Decimal = Decimal("50"), limit: Decimal | None = Decimal("0.4")) -> ExitIntent:
    return ExitIntent(
        token_id=TOKEN,
        side=Side.SELL,
        size=size,
        limit_price=limit,
        order_style=OrderStyle.GTC,  # planner must override to FAK
        urgency=URGENCY_URGENT,
    )


def _store_with_book(*, stale: bool = False) -> MarketStateStore:
    store = MarketStateStore(default_max_age_s=5.0)
    ts = utc_now() - timedelta(seconds=60) if stale else utc_now()
    store.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.49"), Decimal("100")), (Decimal("0.48"), Decimal("200"))],
            asks=[(Decimal("0.51"), Decimal("100"))],
            ts=ts,
        )
    )
    return store


def _planner(**kw) -> ExecutionPlanner:
    cfg = ExecutionPlannerConfig(enabled=True, max_book_age_s=5.0, **kw)
    return ExecutionPlanner(cfg)


# --- entries ---------------------------------------------------------------
def test_passive_entry_plans_gtc() -> None:
    res = _planner().plan(_approved(_entry(urgency=URGENCY_PASSIVE)))
    assert res.approved
    assert res.plan.order_style == OrderStyle.GTC
    assert res.plan.limit_price == Decimal("0.5")
    assert res.reason == rc.PLANNER_PASSIVE_ENTRY


def test_normal_entry_plans_gtc() -> None:
    res = _planner().plan(_approved(_entry(style=OrderStyle.GTC)))
    assert res.approved
    assert res.plan.order_style == OrderStyle.GTC
    assert res.reason == rc.PLANNER_NORMAL_ENTRY


def test_fak_entry_plans_fak_not_gtc() -> None:
    res = _planner().plan(
        _approved(_entry(style=OrderStyle.FAK, limit=Decimal("0.55"))),
        market_state=_store_with_book(),
    )
    assert res.approved
    assert res.plan.order_style == OrderStyle.FAK
    assert res.reason == rc.PLANNER_PAIRED_ENTRY_FAK


def test_passive_entry_works_without_fresh_book() -> None:
    # No market_state at all; entry still plans because the limit price is known.
    res = _planner().plan(_approved(_entry()), market_state=None)
    assert res.approved
    assert res.plan.order_style == OrderStyle.GTC


# --- urgent / protection exits --------------------------------------------
def test_urgent_exit_plans_fak() -> None:
    res = _planner().plan(_approved(_urgent_exit()), market_state=_store_with_book())
    assert res.approved
    assert res.plan.order_style == OrderStyle.FAK
    assert res.reason == rc.PLANNER_URGENT_EXIT_FAK


def test_urgent_exit_worst_price_from_book() -> None:
    # SELL 50 fits at top bid 0.49 → worst acceptable == 0.49.
    res = _planner().plan(_approved(_urgent_exit(size=Decimal("50"))), market_state=_store_with_book())
    assert res.approved
    assert res.plan.limit_price == Decimal("0.49")


def test_stale_book_blocks_urgent_exit() -> None:
    res = _planner().plan(_approved(_urgent_exit()), market_state=_store_with_book(stale=True))
    assert not res.approved
    assert res.reason == rc.PLANNER_STALE_BOOK


def test_missing_book_blocks_urgent_exit() -> None:
    res = _planner().plan(_approved(_urgent_exit()), market_state=MarketStateStore())
    assert not res.approved
    assert res.reason == rc.PLANNER_MISSING_BOOK


def test_urgent_exit_no_market_data_denies_without_fallback() -> None:
    res = _planner().plan(_approved(_urgent_exit()), market_state=None)
    assert not res.approved
    assert res.reason == rc.PLANNER_NO_MARKET_DATA


def test_urgent_exit_fallback_when_enabled() -> None:
    res = _planner(allow_urgent_exit_fallback=True).plan(
        _approved(_urgent_exit(limit=Decimal("0.4"))), market_state=None
    )
    assert res.approved
    assert res.plan.order_style == OrderStyle.FAK
    assert res.reason == rc.PLANNER_URGENT_EXIT_FALLBACK
    assert res.plan.limit_price == Decimal("0.4")


def test_planner_preserves_client_order_id() -> None:
    ap = _approved(_entry())
    res = _planner().plan(ap)
    assert res.plan.client_order_id == ap.client_order_id
    assert res.plan.intent.intent_id == ap.intent.intent_id


def test_no_gtd_or_post_only_style_accepted() -> None:
    # The planner only ever emits GTC or FAK; OrderStyle has no GTD/post-only.
    for urgency in (URGENCY_PASSIVE, URGENCY_NORMAL):
        res = _planner().plan(_approved(_entry(urgency=urgency)))
        assert res.plan.order_style in (OrderStyle.GTC, OrderStyle.FAK)
    res = _planner().plan(_approved(_urgent_exit()), market_state=_store_with_book())
    assert res.plan.order_style == OrderStyle.FAK


# --- integration through the pipeline -------------------------------------
_RISK = {
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


def _runtime(planner_enabled: bool) -> dict:
    rt = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
        "logging": {"level": "WARNING"},
    }
    if planner_enabled:
        rt["market_data"] = {"enabled": True, "max_book_age_s": 5}
        rt["execution"] = {"planner": {"enabled": True}}
    return rt


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    coord.market_state = MarketStateStore()
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    return coord


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _run(tmp_path: Path, planner_enabled: bool) -> list[dict]:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime=_runtime(planner_enabled),
    )
    coord = _coord(tmp_path)
    facts = tmp_path / f"facts-{uuid4()}.jsonl"
    sig = SimpleSignal(
        token_id=TOKEN,
        side=Side.BUY,
        order_style=OrderStyle.GTC,
        dedup_key="plan-1",
        notional_usd=Decimal("5"),
        limit_price=Decimal("0.5"),
    )
    with JsonlSink(facts) as sink:
        asyncio.run(
            process_signals(
                [sig],
                app=app,
                run_id=RunId(str(uuid4())),
                strategy=SimpleSignalTestStrategy(),
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
            )
        )
    return _read(facts)


def test_planner_fact_emitted(tmp_path: Path) -> None:
    rows = _run(tmp_path, planner_enabled=True)
    plan_facts = [r for r in rows if r["fact_type"] == FACT_TYPE_EXECUTION_PLAN]
    assert len(plan_facts) == 1
    assert plan_facts[0]["payload"]["approved"] is True
    assert plan_facts[0]["payload"]["execution_style"] == "GTC"


def test_planner_changes_do_not_skip_risk(tmp_path: Path) -> None:
    rows = _run(tmp_path, planner_enabled=True)
    risk = [r for r in rows if r["fact_type"] == FACT_TYPE_RISK]
    # Two risk_decision facts: pre-check + planned.
    phases = [r["payload"].get("phase") for r in risk]
    assert "planned" in phases
    assert any(p is None for p in phases)  # the pre-check has no phase key


def test_planned_order_revalidated_before_oms(tmp_path: Path) -> None:
    rows = _run(tmp_path, planner_enabled=True)
    order = [r["fact_type"] for r in rows]
    i_plan = order.index(FACT_TYPE_EXECUTION_PLAN)
    i_planned_risk = max(
        idx for idx, r in enumerate(rows) if r["fact_type"] == FACT_TYPE_RISK
    )
    i_submit = order.index(FACT_TYPE_OMS_SUBMIT)
    assert i_plan < i_planned_risk < i_submit


def test_oms_submit_carries_planner_reason(tmp_path: Path) -> None:
    rows = _run(tmp_path, planner_enabled=True)
    submit = [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT][0]
    assert submit["payload"].get("planner_reason") == rc.PLANNER_NORMAL_ENTRY


def test_planner_disabled_falls_back_to_intent_style(tmp_path: Path) -> None:
    rows = _run(tmp_path, planner_enabled=False)
    assert all(r["fact_type"] != FACT_TYPE_EXECUTION_PLAN for r in rows)
    submit = [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT][0]
    assert "planner_reason" not in submit["payload"]


def test_planner_enabled_requires_market_data_provider() -> None:
    from tyrex_pm.runtime.config import ConfigError

    with pytest.raises(ConfigError):
        parse_app_config(
            risk=dict(_RISK),
            strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
            runtime={
                "execution_mode": "shadow",
                "shadow_bootstrap": {"usdc_balance": "1", "usdc_allowance": "1"},
                "market_data": {"enabled": False},
                "execution": {"planner": {"enabled": True}},
            },
        )
