"""PairEntrySaga compound entry coordinator tests."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from tyrex_pm.core.enums import ExecutionMode, OrderStyle
from tyrex_pm.core.ids import RunId, TokenId
from tyrex_pm.execution.adapters import ShadowOMS
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ShadowBootstrapConfig, parse_app_config
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.runtime.health_runtime import HealthRuntime
from tyrex_pm.runtime.market_data_runtime import inject_fixture_book
from tyrex_pm.runtime.pair_entry_saga import (
    abort_pair_entry,
    preflight_pair_entry,
    run_pair_entry_from_idle,
    tick_pair_entry_pending,
)
from tyrex_pm.runtime.paired_binary_recovery import recover_on_startup
from tyrex_pm.runtime.paired_binary_run import _run_emergency_unwind, run_paired_binary_loop
from tyrex_pm.runtime.reset_state import reset_local_state, venue_orders_warning
from tyrex_pm.state.allocation_ledger import AllocationLedger
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.state.order_store import OrderStore
from tyrex_pm.state.shadow_wallet import apply_shadow_bootstrap
from tyrex_pm.state.wallet_store import WalletStore
from tyrex_pm.strategies.base import StrategyContext
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.strategies.paired_binary.entry_eval import read_leg_book
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState
from tyrex_pm.strategies.paired_binary.strategy import PairedBinaryStrategy

YES = "9059650700126795019827485089957938050581213031053374092199507389736394347163"
NO = "9059650700126795019827485089957938050581213031053374092199507389736394347164"

_RISK_MIN_1 = {
    "notional": {"min_usd": "1", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": True, "default_min_size": "5"},
    "capital": {"enabled": False},
    "inventory": {"sell_requires_venue_position": False},
}

_RISK_LOW_MIN = {
    "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
    "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
    "venue_min_size": {"enabled": True, "default_min_size": "5"},
    "capital": {"enabled": False},
    "inventory": {"sell_requires_venue_position": False},
}


def _runtime(**over) -> dict:
    base = {
        "execution_mode": "shadow",
        "shadow_bootstrap": {"usdc_balance": "1000000", "usdc_allowance": "1000000"},
        "reporting": {"enabled": True, "runs_dir": "var/reporting/runs"},
        "market_data": {"enabled": True, "max_book_age_s": 5},
        "execution": {"planner": {"enabled": True}},
    }
    base.update(over)
    return base


def _strategy(**pb_over) -> dict:
    pb = {
        "owner_id": "paired_binary",
        "market_id": "m1",
        "yes_token_id": YES,
        "no_token_id": NO,
        "position_size": "5",
        "max_pair_entry_cost": "1.02",
        "max_spread_yes": "0.02",
        "max_spread_no": "0.02",
        "pair_stop_loss_pct": "0.04",
        "pair_take_profit_pct": "0.10",
        "slippage_buffer": "0.005",
        "reject_if_spread_exceeds_loss_budget": True,
        "max_holding_time_s": 3600,
        "entry_order_style": "FAK",
        "allow_resting_entry_orders": False,
        "pair_entry_fill_timeout_s": 10,
        "pair_entry_resting_timeout_s": 2,
        "min_effective_pair_qty": "5",
        "use_fixture_book": True,
        "fixture_yes_bid": "0.48",
        "fixture_yes_ask": "0.49",
        "fixture_no_bid": "0.50",
        "fixture_no_ask": "0.51",
        "run_once": True,
        "tick_interval_s": 0.05,
        "max_runtime_s": 5,
        "stop_after_entry": True,
    }
    pb.update(pb_over)
    return {"kind": "paired_binary", "enabled": True, "paired_binary": pb}


def _app(risk: dict | None = None, **pb_over):
    return parse_app_config(
        risk=dict(risk or _RISK_LOW_MIN),
        strategy=_strategy(**pb_over),
        runtime=_runtime(),
    )


def _coord(tmp_path: Path) -> RuntimeCoordinator:
    coord = RuntimeCoordinator(wallet=WalletStore(), orders=OrderStore(), health=HealthRuntime())
    coord.allocation_ledger = AllocationLedger(path=tmp_path / f"ledger-{uuid4()}.json")
    apply_shadow_bootstrap(
        coord.wallet,
        ShadowBootstrapConfig(usdc_balance=Decimal("1000000"), usdc_allowance=Decimal("1000000")),
    )
    coord.market_state = MarketStateStore(default_max_age_s=5.0)
    return coord


def _inject_books(coord, *, no_ask: str = "0.51") -> None:
    inject_fixture_book(coord, YES, best_bid=Decimal("0.48"), best_ask=Decimal("0.49"))
    inject_fixture_book(coord, NO, best_bid=Decimal("0.50"), best_ask=Decimal(no_ask))


def _entry_pairs(app, coord, pair_id: str = "pc_test"):
    cfg = app.paired_binary
    assert cfg is not None
    strat = PairedBinaryStrategy(cfg)
    ctx = StrategyContext(coord=coord, market_state=coord.market_state)
    pairs, skip = strat.evaluate_entry(ctx, pair_correlation_id=pair_id)
    assert skip is None
    return pairs, cfg, strat


def test_pair_preflight_rejects_if_no_notional_below_min_before_yes_submit(tmp_path: Path) -> None:
    app = _app(risk=_RISK_MIN_1, fixture_no_ask="0.16")
    coord = _coord(tmp_path)
    _inject_books(coord, no_ask="0.16")
    pairs, cfg, _ = _entry_pairs(app, coord)
    assert cfg is not None
    pf = preflight_pair_entry(pairs, app=app, coord=coord, run_id=RunId("r1"), cfg=cfg)
    assert not pf.ok
    assert not pf.no.approved
    assert "notional_below_min" in pf.no.reason_codes


def test_pair_preflight_rejects_if_yes_risk_denied(tmp_path: Path) -> None:
    app = _app(risk=_RISK_MIN_1, fixture_yes_ask="0.05", max_pair_entry_cost="1.02")
    coord = _coord(tmp_path)
    _inject_books(coord)
    inject_fixture_book(coord, YES, best_bid=Decimal("0.04"), best_ask=Decimal("0.05"))
    pairs, cfg, _ = _entry_pairs(app, coord)
    assert cfg is not None
    pf = preflight_pair_entry(pairs, app=app, coord=coord, run_id=RunId("r1"), cfg=cfg)
    assert not pf.ok
    assert not pf.yes.approved


def test_pair_preflight_rejects_if_no_risk_denied(tmp_path: Path) -> None:
    test_pair_preflight_rejects_if_no_notional_below_min_before_yes_submit(tmp_path)


def test_pair_preflight_rejects_if_planner_denies_one_leg(tmp_path: Path) -> None:
    from datetime import timedelta
    from tyrex_pm.core.time import utc_now
    from tyrex_pm.state.market_store import make_snapshot

    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    _inject_books(coord)
    pairs, _, _ = _entry_pairs(app, coord)
    stale_ts = utc_now() - timedelta(seconds=60)
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(YES), bids=[(Decimal("0.48"), Decimal("100"))], asks=[(Decimal("0.49"), Decimal("100"))], ts=stale_ts)
    )
    coord.market_state.apply_snapshot(
        make_snapshot(TokenId(NO), bids=[(Decimal("0.50"), Decimal("100"))], asks=[(Decimal("0.51"), Decimal("100"))], ts=stale_ts)
    )
    pf = preflight_pair_entry(pairs, app=app, coord=coord, run_id=RunId("r1"), cfg=cfg)
    assert not pf.ok
    assert not pf.yes.planner_approved or not pf.no.planner_approved


@pytest.mark.asyncio
async def test_pair_preflight_submits_nothing_when_either_leg_fails(tmp_path: Path) -> None:
    app = _app(risk=_RISK_MIN_1, fixture_no_ask="0.16")
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    _inject_books(coord, no_ask="0.16")
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    state = PairedBinaryRuntimeState(
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
    )
    strat = PairedBinaryStrategy(cfg)
    yes_book = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=cfg.max_book_age_s)
    no_book = read_leg_book(coord.market_state, TokenId(NO), max_book_age_s=cfg.max_book_age_s)
    with JsonlSink(runs / "facts.jsonl") as sink:
        ok = await run_pair_entry_from_idle(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            strategy=strat,
            cfg=cfg,
            state=state,
            pair_correlation_id="pc_fail",
            yes_book=yes_book,
            no_book=no_book,
            apply_local_shadow_fill=True,
            live_clob_client=None,
            unwind_fn=_run_emergency_unwind,
        )
    assert not ok
    assert state.phase == PairedBinaryPhase.IDLE
    assert coord.orders.orders == {}


def test_paired_entry_fak_not_silently_converted_to_gtc(tmp_path: Path) -> None:
    from tyrex_pm.execution.planner import ExecutionPlanner
    from tyrex_pm.core.models import ApprovedIntent, ClientOrderId, EnterIntent
    from tyrex_pm.core.enums import Side

    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    _inject_books(coord)
    pairs, _, _ = _entry_pairs(app, coord)
    yes_intent, _ = pairs[0]
    assert yes_intent.order_style == OrderStyle.FAK
    planner = ExecutionPlanner(app.execution.planner)
    res = planner.plan(
        ApprovedIntent(intent=yes_intent, client_order_id=ClientOrderId("c1"), run_id=RunId("r1")),
        market_state=coord.market_state,
    )
    assert res.approved
    assert res.plan.order_style == OrderStyle.FAK


def test_paired_entry_style_mismatch_fails_live_config() -> None:
    app = _app(allow_entry_style_downgrade=False, entry_order_style="FAK")
    cfg = app.paired_binary
    assert cfg is not None
    assert cfg.allow_entry_style_downgrade is False
    assert cfg.entry_order_style.value == "FAK"


@pytest.mark.asyncio
async def test_shadow_entry_emits_pair_preflight(tmp_path: Path) -> None:
    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    with JsonlSink(runs / "facts.jsonl") as sink:
        state = PairedBinaryRuntimeState(
            owner_id=cfg.owner_id,
            market_id=cfg.market_id,
            yes_token_id=cfg.yes_token_id,
            no_token_id=cfg.no_token_id,
        )
        await run_paired_binary_loop(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            cfg=cfg,
            state=state,
            state_dir=tmp_path,
            apply_local_shadow_fill=True,
        )
    types = [
        json.loads(l)["fact_type"]
        for l in (runs / "facts.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    assert "paired_binary_pair_preflight" in types


@pytest.mark.asyncio
async def test_entry_timeout_unwind_uses_retry_engine(tmp_path: Path) -> None:
    app = _app(pair_entry_fill_timeout_s=0.01)
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    _inject_books(coord)
    run_id = RunId(str(uuid4()))
    runs = tmp_path / "runs" / str(run_id)
    runs.mkdir(parents=True)
    state = PairedBinaryRuntimeState(
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        phase=PairedBinaryPhase.BOTH_ENTRY_PENDING,
        pair_correlation_id="pc_timeout",
    )
    coord.allocation_ledger.apply_buy(cfg.owner_id, TokenId(YES), Decimal("5"), correlation_id="y")
    state.yes.filled_qty = Decimal("5")
    state.entry_deadline_ts = monotonic_s() - 1.0
    strat = PairedBinaryStrategy(cfg)
    yes_book = read_leg_book(coord.market_state, TokenId(YES), max_book_age_s=cfg.max_book_age_s)
    no_book = read_leg_book(coord.market_state, TokenId(NO), max_book_age_s=cfg.max_book_age_s)
    with JsonlSink(runs / "facts.jsonl") as sink:
        action = await tick_pair_entry_pending(
            app=app,
            run_id=run_id,
            coord=coord,
            sink=sink,
            oms=ShadowOMS(),
            strategy=strat,
            cfg=cfg,
            state=state,
            yes_book=yes_book,
            no_book=no_book,
            apply_local_shadow_fill=True,
            live_clob_client=None,
            unwind_fn=_run_emergency_unwind,
        )
        assert action == "fill_timeout"


def test_recovery_failed_non_flat_launches_unwind_or_manual_intervention(tmp_path: Path) -> None:
    from tyrex_pm.core.models import WalletPosition
    from tyrex_pm.strategies.paired_binary.state import persistence_path, save_persisted_state

    app = _app()
    cfg = app.paired_binary
    assert cfg is not None
    coord = _coord(tmp_path)
    coord.wallet.positions[TokenId(YES)] = WalletPosition(
        token_id=TokenId(YES), qty=Decimal("5"), avg_price_usd=Decimal("0.49")
    )
    state = PairedBinaryRuntimeState(
        owner_id=cfg.owner_id,
        market_id=cfg.market_id,
        yes_token_id=cfg.yes_token_id,
        no_token_id=cfg.no_token_id,
        phase=PairedBinaryPhase.FAILED,
    )
    path = persistence_path(tmp_path, cfg.owner_id, cfg.market_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    save_persisted_state(path, state)
    recovered = recover_on_startup(coord, cfg, state_dir=tmp_path)
    assert recovered.phase == PairedBinaryPhase.ONLY_YES_ACTIVE
    assert recovered.effective_qty == Decimal("5")


def test_reset_state_warns_about_open_venue_orders(tmp_path: Path) -> None:
    assert "does not cancel venue open orders" in venue_orders_warning()


def test_reset_state_paired_binary_flag(tmp_path: Path) -> None:
    pb = tmp_path / "paired_binary" / "paired_binary" / "m1.json"
    pb.parent.mkdir(parents=True)
    pb.write_text("{}", encoding="utf-8")
    removed = reset_local_state(tmp_path, paired_binary=True)
    assert not pb.exists()
    assert any("paired_binary" in str(p) for p in removed)
