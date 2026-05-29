"""Unit tests for the allocation_test strategy state machine."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import OrderStyle, Side
from tyrex_pm.core.errors import ConfigError
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.models import EnterIntent, WalletPosition
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.allocation_ids import (
    ALLOCATION_TEST_INTENT_SOURCE,
    DEFAULT_ALLOCATION_TEST_OWNER_A,
    DEFAULT_ALLOCATION_TEST_OWNER_B,
)
from tyrex_pm.runtime.allocation_runtime import clamp_planned_to_allocated, resolve_owner_id
from tyrex_pm.runtime.config import SELL_TEST_PRICING_AUTO, load_app_config, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.allocation_test.strategy import (
    PHASE_DONE,
    PHASE_OWNER_A_ALLOCATION_VISIBLE,
    PHASE_OWNER_A_BUY,
    PHASE_OWNER_A_BUY_SUBMITTED,
    PHASE_OWNER_A_SELL,
    PHASE_OWNER_A_SELL_SUBMITTED,
    PHASE_OWNER_B_SELL_BLOCKED,
    AllocationTestStrategy,
)


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
    "allocation_ledger": {"enabled": True},
}


def _allocation_test_strategy_dict(token_id: str = "tok-alloc-test") -> dict:
    return {
        "kind": "allocation_test",
        "enabled": True,
        "token_id": token_id,
        "owner_a_id": DEFAULT_ALLOCATION_TEST_OWNER_A,
        "owner_b_id": DEFAULT_ALLOCATION_TEST_OWNER_B,
        "buy": {"enabled": True, "notional_usd": "5", "limit_price": "0.50"},
        "owner_b_unauthorized_sell": {"enabled": True, "size_mode": "match_owner_a_buy"},
        "owner_a_sell": {
            "enabled": True,
            "delay_s": 0,
            "pricing_mode": "auto",
            "aggression_ticks": 0,
            "limit_price": "0.01",
        },
        "run_once": True,
    }


def _wire_coord(tmp_path: Path) -> tuple[RuntimeCoordinator, JsonlSink, str, AllocationLedger]:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    run_id = str(uuid4())
    sink = JsonlSink(tmp_path / "facts.jsonl")
    sink.__enter__()
    ledger = AllocationLedger(path=tmp_path / "allocation_ledger.json")
    coord.allocation_ledger = ledger
    return coord, sink, run_id, ledger


def test_parse_allocation_test_yaml_file() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    app = load_app_config(
        repo_root=root,
        strategy_file="config/strategies/allocation_test.yaml",
    )
    assert app.allocation_test is not None
    assert app.allocation_test.owner_a_sell.pricing_mode == SELL_TEST_PRICING_AUTO
    assert app.allocation_test.owner_a_sell.aggression_ticks == 0
    assert app.allocation_test.owner_a_sell.limit_price == Decimal("0.01")


def test_parse_allocation_test_strategy_yaml() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    assert app.sell_test is None
    assert app.allocation_test.token_id == "tok-alloc-test"
    assert app.allocation_test.owner_a_id == DEFAULT_ALLOCATION_TEST_OWNER_A
    assert app.allocation_test.buy.notional_usd == Decimal("5")
    assert app.allocation_test.owner_a_sell.pricing_mode == SELL_TEST_PRICING_AUTO
    assert app.allocation_test.owner_a_sell.aggression_ticks == 0
    assert app.allocation_test.owner_a_sell.limit_price == Decimal("0.01")


def test_parse_allocation_test_requires_distinct_owners() -> None:
    bad = _allocation_test_strategy_dict()
    bad["owner_b_id"] = bad["owner_a_id"]
    with pytest.raises(ConfigError):
        parse_app_config(risk=dict(_BASE_RISK), strategy=bad, runtime=dict(_BASE_RUNTIME))


def test_owner_a_buy_work_unit_has_allocation_owner_id() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    units = strat.owner_a_buy_work_units()
    assert len(units) == 1
    ext = units[0].intent_fact_extensions
    assert ext["source"] == ALLOCATION_TEST_INTENT_SOURCE
    assert ext["allocation_owner_id"] == DEFAULT_ALLOCATION_TEST_OWNER_A
    assert ext["allocation_test_phase"] == PHASE_OWNER_A_BUY
    assert isinstance(units[0].intent, EnterIntent)
    assert units[0].intent.side == Side.BUY


def test_resolve_owner_id_uses_allocation_owner_id_extension() -> None:
    intent = EnterIntent(
        token_id=TokenId("tok"),
        side=Side.BUY,
        size=Decimal("10"),
        limit_price=Decimal("0.5"),
        order_style=OrderStyle.GTC,
    )
    strat = AllocationTestStrategy(
        parse_app_config(
            risk=dict(_BASE_RISK),
            strategy=_allocation_test_strategy_dict(),
            runtime=dict(_BASE_RUNTIME),
        ).allocation_test  # type: ignore[arg-type]
    )
    owner = resolve_owner_id(
        strat,
        intent,
        intent_extensions={"allocation_owner_id": "custom_owner_A"},
    )
    assert owner == "custom_owner_A"


def test_owner_b_clamp_zero_skips_work_unit(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
    )
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("10"), correlation_id="buy-a")
    strat._phase = PHASE_OWNER_A_ALLOCATION_VISIBLE  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("10")  # noqa: SLF001 — test setup

    blocked = strat.attempt_owner_b_unauthorized_sell(coord, sink, run_id)
    assert blocked is True
    assert strat.phase == PHASE_OWNER_B_SELL_BLOCKED
    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    assert wu.intent.size <= Decimal("10")
    sink.__exit__(None, None, None)


def test_owner_b_blocked_emits_health_with_wallet_gt_zero(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("10"), avg_price_usd=Decimal("0.5")
    )
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("10"), correlation_id="buy-a")
    strat._phase = PHASE_OWNER_A_ALLOCATION_VISIBLE  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("10")  # noqa: SLF001

    strat.attempt_owner_b_unauthorized_sell(coord, sink, run_id)
    facts = [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    blocked = [
        r
        for r in facts
        if r["fact_type"] == FACT_TYPE_HEALTH
        and r["payload"].get("event") == "allocation_test_unauthorized_sell_blocked"
    ]
    assert len(blocked) == 1
    payload = blocked[0]["payload"]
    assert Decimal(payload["wallet_position_qty"]) > 0
    assert Decimal(payload["allocated_available"]) == 0
    assert payload["reason"] == "insufficient_allocation"
    assert payload["owner_id"] == DEFAULT_ALLOCATION_TEST_OWNER_B
    sink.__exit__(None, None, None)


def test_owner_a_sell_size_clamped_to_allocated(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("20"), avg_price_usd=Decimal("0.5")
    )
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("8"), correlation_id="buy-a")
    strat._phase = PHASE_OWNER_B_SELL_BLOCKED  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("10")  # noqa: SLF001

    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    assert wu.intent.size == Decimal("8")
    assert wu.intent_fact_extensions["allocation_test_phase"] == PHASE_OWNER_A_SELL
    clamped = clamp_planned_to_allocated(
        coord,
        owner_id=DEFAULT_ALLOCATION_TEST_OWNER_B,
        token_id=tid,
        planned=Decimal("10"),
    )
    assert clamped == Decimal("0")
    sink.__exit__(None, None, None)


def test_is_done_only_after_terminal_state() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    assert not strat.is_done()
    strat._phase = PHASE_OWNER_A_SELL_SUBMITTED  # noqa: SLF001
    strat.notify_sell_submitted({"match_status": "matched"}, shadow_instant_fill=True)
    assert strat.is_done()
    assert strat.phase == PHASE_DONE


def test_owner_b_waits_until_wallet_position_visible(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("8"), correlation_id="buy-a")
    strat._buy_submit_succeeded = True  # noqa: SLF001
    strat._phase = PHASE_OWNER_A_BUY_SUBMITTED  # noqa: SLF001

    assert not strat.check_owner_b_prerequisites_visible(coord)

    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("8"), avg_price_usd=Decimal("0.5")
    )
    assert strat.check_owner_b_prerequisites_visible(coord)

    strat._phase = PHASE_OWNER_A_ALLOCATION_VISIBLE  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("8")  # noqa: SLF001
    strat.attempt_owner_b_unauthorized_sell(coord, sink, run_id)
    facts = [
        json.loads(ln)
        for ln in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    blocked = next(
        r for r in facts if r["payload"].get("event") == "allocation_test_unauthorized_sell_blocked"
    )
    assert Decimal(blocked["payload"]["wallet_position_qty"]) > 0
    assert Decimal(blocked["payload"]["allocated_available"]) == 0
    sink.__exit__(None, None, None)


def test_is_done_on_buy_denied() -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    strat.owner_a_buy_work_units()
    strat.notify_buy_not_submitted()
    assert strat.is_done()
    assert strat.is_terminal_failure()


def test_owner_a_sell_uses_resolved_auto_price(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("8"), avg_price_usd=Decimal("0.5")
    )
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("8"), correlation_id="buy-a")
    strat._phase = PHASE_OWNER_B_SELL_BLOCKED  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("8")  # noqa: SLF001
    strat.set_resolved_sell_price(
        Decimal("0.47"),
        evidence={"source": "auto_book", "best_bid": "0.48", "tick_size": "0.01"},
    )

    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    assert wu.intent.limit_price == Decimal("0.47")
    assert wu.intent_fact_extensions["allocation_test_sell_pricing_mode"] == SELL_TEST_PRICING_AUTO
    assert wu.intent_fact_extensions["allocation_test_sell_pricing"]["source"] == "auto_book"
    sink.__exit__(None, None, None)


def test_owner_a_sell_fixed_pricing_mode(tmp_path: Path) -> None:
    strat_dict = _allocation_test_strategy_dict()
    strat_dict["owner_a_sell"] = {
        "enabled": True,
        "delay_s": 0,
        "pricing_mode": "fixed",
        "limit_price": "0.49",
    }
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=strat_dict,
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("8"), avg_price_usd=Decimal("0.5")
    )
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("8"), correlation_id="buy-a")
    strat._phase = PHASE_OWNER_B_SELL_BLOCKED  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("8")  # noqa: SLF001

    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    assert wu.intent.limit_price == Decimal("0.49")
    sink.__exit__(None, None, None)


def test_owner_a_sell_auto_shadow_uses_fallback_limit(tmp_path: Path) -> None:
    """Shadow (no live resolution): auto mode falls back to configured limit_price."""
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    coord, sink, run_id, ledger = _wire_coord(tmp_path)
    tid = TokenId(app.allocation_test.token_id)
    coord.wallet.positions[tid] = WalletPosition(
        token_id=tid, qty=Decimal("8"), avg_price_usd=Decimal("0.5")
    )
    ledger.apply_buy(DEFAULT_ALLOCATION_TEST_OWNER_A, tid, Decimal("8"), correlation_id="buy-a")
    strat._phase = PHASE_OWNER_B_SELL_BLOCKED  # noqa: SLF001
    strat._owner_a_buy_size = Decimal("8")  # noqa: SLF001

    wu = strat.build_owner_a_sell_work_unit(coord)
    assert wu is not None
    assert wu.intent.limit_price == Decimal("0.01")
    sink.__exit__(None, None, None)


def test_emit_sell_pricing_failed_is_terminal(tmp_path: Path) -> None:
    app = parse_app_config(
        risk=dict(_BASE_RISK),
        strategy=_allocation_test_strategy_dict(),
        runtime=dict(_BASE_RUNTIME),
    )
    assert app.allocation_test is not None
    strat = AllocationTestStrategy(app.allocation_test)
    _, sink, run_id, _ = _wire_coord(tmp_path)

    strat.emit_sell_pricing_failed(sink, run_id, error="no_bids")
    assert strat.is_done()
    assert strat.is_terminal_failure()
    sink.__exit__(None, None, None)
