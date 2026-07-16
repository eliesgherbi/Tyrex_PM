"""Phase 4 (architecture_enhance): ProtectionEngine tests."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.core.models import URGENCY_URGENT, WalletPosition
from tyrex_pm.core.time import utc_now
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.protection.config import ProtectionPolicy, SIZE_MODE_FULL
from tyrex_pm.protection.monitor import ProtectionMonitor
from tyrex_pm.protection.registry import ProtectionRegistry, register_if_allocation_final
from tyrex_pm.reporting.schema_v2 import (
    FACT_TYPE_EXECUTION_PLAN,
    FACT_TYPE_OMS_SUBMIT,
    FACT_TYPE_PROTECTION_REGISTER,
    FACT_TYPE_PROTECTION_TICK,
    FACT_TYPE_PROTECTION_TRIGGER,
    FACT_TYPE_RISK,
)
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_ids import OWNER_PROTECTION
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.pipeline import process_intent_work_unit
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore

TOKEN = TokenId("token-prot")
CORR = "prot-corr-1"

_RISK = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": False},
    "capital": {"enabled": False, "max_wallet_age_s": 120},
    "concurrency": {"max_orders_in_flight": 8},
    "readiness": {
        "require_wallet_sync": False,
        "max_wallet_age_s_live": 120,
        "require_heartbeat_live": False,
        "require_user_ws_live": False,
    },
    "inventory": {"sell_requires_venue_position": True},
}


def _runtime(planner: bool = True) -> dict:
    rt = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "supervisors": {"reconcile_interval_s": 30, "submit_grace_s": 15},
        "logging": {"level": "WARNING"},
    }
    if planner:
        rt["market_data"] = {"enabled": True, "max_book_age_s": 5}
        rt["execution"] = {"planner": {"enabled": True}}
    return rt


def _policy(**kw) -> ProtectionPolicy:
    base = dict(
        take_profit_pct=Decimal("0.20"),
        stop_loss_pct=Decimal("0.20"),
        size_mode=SIZE_MODE_FULL,
        exit_order_style=OrderStyle.FAK,
        max_book_age_s=5.0,
    )
    base.update(kw)
    return ProtectionPolicy(**base)


def _coord(tmp_path: Path, *, with_book: Decimal | None = None, stale: bool = False) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    if with_book is not None:
        ts = utc_now() - timedelta(seconds=60) if stale else utc_now()
        coord.market_state.apply_snapshot(
            make_snapshot(
                TOKEN,
                bids=[(with_book, Decimal("1000"))],
                asks=[(with_book + Decimal("0.02"), Decimal("1000"))],
                ts=ts,
            )
        )
    return coord


def _credit(coord: RuntimeCoordinator, qty: Decimal) -> None:
    coord.allocation_ledger.apply_buy(OWNER_PROTECTION, TOKEN, qty)
    coord.wallet.positions[TOKEN] = WalletPosition(token_id=TOKEN, qty=qty, avg_price_usd=Decimal("0.5"))


def _read(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# --- registration boundary -------------------------------------------------
def test_protection_registers_after_allocation_buy_applied() -> None:
    reg = ProtectionRegistry()
    entry = register_if_allocation_final(
        reg,
        status="CONFIRMED",
        owner_id=OWNER_PROTECTION,
        token_id=TOKEN,
        entry_price=Decimal("0.5"),
        policy=_policy(),
        parent_correlation_id=CORR,
    )
    assert entry is not None
    assert reg.get(OWNER_PROTECTION, TOKEN) is not None


def test_protection_does_not_register_on_submit_ack() -> None:
    reg = ProtectionRegistry()
    # An OMS submit ack is not a trade status → unknown → not allocation final.
    entry = register_if_allocation_final(
        reg, status="submit_ack", owner_id=OWNER_PROTECTION, token_id=TOKEN,
        entry_price=Decimal("0.5"), policy=_policy(), parent_correlation_id=CORR,
    )
    assert entry is None
    assert len(reg) == 0


def test_protection_does_not_register_on_resting_buy() -> None:
    reg = ProtectionRegistry()
    entry = register_if_allocation_final(
        reg, status="LIVE", owner_id=OWNER_PROTECTION, token_id=TOKEN,
        entry_price=Decimal("0.5"), policy=_policy(), parent_correlation_id=CORR,
    )
    assert entry is None


def test_protection_does_not_register_on_matched_only() -> None:
    reg = ProtectionRegistry()
    for status in ("MATCHED", "MINED", "RETRYING", "FAILED"):
        entry = register_if_allocation_final(
            reg, status=status, owner_id=OWNER_PROTECTION, token_id=TOKEN,
            entry_price=Decimal("0.5"), policy=_policy(), parent_correlation_id=CORR,
        )
        assert entry is None, status
    assert len(reg) == 0


# --- triggers --------------------------------------------------------------
def test_take_profit_triggers_exit_intent(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.65"))  # entry 0.5, TP at 0.60
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    work = mon.tick(coord)
    assert len(work) == 1
    assert work[0].intent.side == Side.SELL
    assert work[0].intent.urgency == URGENCY_URGENT
    assert work[0].intent_fact_extensions["protection_trigger"] == "take_profit"


def test_stop_loss_triggers_exit_intent(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.35"))  # entry 0.5, SL at 0.40
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    work = mon.tick(coord)
    assert len(work) == 1
    assert work[0].intent_fact_extensions["protection_trigger"] == "stop_loss"


def test_protection_exit_clamps_to_owner_allocation(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.65"))
    # Allocation only 3 even though wallet has 100.
    coord.allocation_ledger.apply_buy(OWNER_PROTECTION, TOKEN, Decimal("3"))
    coord.wallet.positions[TOKEN] = WalletPosition(token_id=TOKEN, qty=Decimal("100"), avg_price_usd=Decimal("0.5"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    work = mon.tick(coord)
    assert len(work) == 1
    assert work[0].intent.size == Decimal("3")


def test_duplicate_trigger_does_not_double_sell(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.65"))
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    first = mon.tick(coord)
    second = mon.tick(coord)
    assert len(first) == 1
    assert second == []  # entry is marked triggered after the first exit


def test_protection_stale_book_does_not_emit_false_trigger(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.65"), stale=True)
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    work = mon.tick(coord)
    assert work == []
    assert mon.registry.get(OWNER_PROTECTION, TOKEN).triggered is False


def test_protection_missing_book_does_not_emit_false_trigger(tmp_path: Path) -> None:
    coord = _coord(tmp_path)  # no book at all
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    assert mon.tick(coord) == []


def test_protection_tick_fact_deduped(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.52"))  # below TP/SL → no trigger
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    facts = tmp_path / f"facts-{uuid4()}.jsonl"
    rid = RunId(str(uuid4()))
    with JsonlSink(facts) as sink:
        mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                     policy=_policy(), parent_correlation_id=CORR, sink=sink, run_id=rid)
        # Three ticks at the SAME price → only one tick fact.
        for _ in range(3):
            mon.tick(coord, sink=sink, run_id=rid)
    rows = _read(facts)
    ticks = [r for r in rows if r["fact_type"] == FACT_TYPE_PROTECTION_TICK]
    assert len(ticks) == 1
    assert any(r["fact_type"] == FACT_TYPE_PROTECTION_REGISTER for r in rows)


def test_protection_non_guru_owner_supported(tmp_path: Path) -> None:
    coord = _coord(tmp_path, with_book=Decimal("0.65"))
    owner = "my_custom_strategy"
    coord.allocation_ledger.apply_buy(owner, TOKEN, Decimal("10"))
    coord.wallet.positions[TOKEN] = WalletPosition(token_id=TOKEN, qty=Decimal("10"), avg_price_usd=Decimal("0.5"))
    mon = ProtectionMonitor(ProtectionRegistry())
    mon.register(owner_id=owner, token_id=TOKEN, entry_price=Decimal("0.5"),
                 policy=_policy(), parent_correlation_id=CORR)
    work = mon.tick(coord)
    assert len(work) == 1
    assert work[0].intent_fact_extensions["allocation_owner_id"] == owner


# --- end-to-end through the generic pipeline -------------------------------
def _run_exit_through_pipeline(tmp_path: Path) -> list[dict]:
    app = parse_app_config(
        risk=dict(_RISK),
        strategy={"kind": "simple_signal_test", "token_id": str(TOKEN)},
        runtime=_runtime(planner=True),
    )
    coord = _coord(tmp_path, with_book=Decimal("0.65"))
    _credit(coord, Decimal("10"))
    mon = ProtectionMonitor(ProtectionRegistry())
    facts = tmp_path / f"facts-{uuid4()}.jsonl"
    rid = RunId(str(uuid4()))
    with JsonlSink(facts) as sink:
        mon.register(owner_id=OWNER_PROTECTION, token_id=TOKEN, entry_price=Decimal("0.5"),
                     policy=_policy(), parent_correlation_id=CORR, sink=sink, run_id=rid)
        work = mon.tick(coord, sink=sink, run_id=rid)
        assert len(work) == 1
        asyncio.run(
            process_intent_work_unit(
                work[0],
                app=app,
                run_id=rid,
                strategy=mon,  # duck-typed: no on_buy_submit_ack needed for a SELL
                coord=coord,
                sink=sink,
                oms=ShadowOMS(),
            )
        )
    return _read(facts)


def test_protection_exit_uses_execution_planner(tmp_path: Path) -> None:
    rows = _run_exit_through_pipeline(tmp_path)
    plan = [r for r in rows if r["fact_type"] == FACT_TYPE_EXECUTION_PLAN]
    assert len(plan) == 1
    assert plan[0]["payload"]["execution_style"] == "FAK"  # urgent exit → FAK


def test_protection_exit_goes_through_risk_engine(tmp_path: Path) -> None:
    rows = _run_exit_through_pipeline(tmp_path)
    risk = [r for r in rows if r["fact_type"] == FACT_TYPE_RISK]
    # pre-check + planned phase.
    phases = [r["payload"].get("phase") for r in risk]
    assert "planned" in phases
    assert any(p is None for p in phases)
    submit = [r for r in rows if r["fact_type"] == FACT_TYPE_OMS_SUBMIT]
    assert len(submit) == 1


def test_protection_trigger_fact_emitted(tmp_path: Path) -> None:
    rows = _run_exit_through_pipeline(tmp_path)
    trig = [r for r in rows if r["fact_type"] == FACT_TYPE_PROTECTION_TRIGGER]
    assert len(trig) == 1
    assert trig[0]["payload"]["trigger"] == "take_profit"
