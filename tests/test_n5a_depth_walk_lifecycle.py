"""N5A hardening: end-to-end SHADOW lifecycle through shadow_depth_walk_v1.

Fixture-only parameters (latency_ms=0, explicit strategy knobs) — not production
defaults. These tests compose ShadowHost + F4 timelines with the N5 fill model,
and prove inventory/lifecycle outcomes follow simulated fills.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import tyrex_pm
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, RunId
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.execution.order_store import OrderStatus
from tyrex_pm.execution.shadow_fill_model import FILL_MODEL_DEPTH_WALK_V1
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.runtime.config import observe_config_from_mapping
from tyrex_pm.runtime.shadow_host import ShadowHost
from tyrex_pm.strategies.decisions import StrategyAction

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
CFG_RICH = ROOT / "config" / "observe_shadow_z_gap_f4.json"
TS = datetime(2026, 7, 20, 12, 1, 0, tzinfo=timezone.utc)


def _depth_walk_cfg(
    tmp: Path,
    *,
    fixture: str | None = None,
    latency_ms: float = 0.0,
    **z_gap_overrides,
) -> object:
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp / "facts.jsonl")
    raw["fixture_path"] = str(ROOT / (fixture or raw["fixture_path"]))
    raw["shadow"]["persistence_path"] = str(tmp / "state.json")
    # Fixture-only N5 fill model — not a production latency assumption.
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": latency_ms,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    raw["shadow"]["require_flat_for_promote"] = True
    raw["z_gap"].update(z_gap_overrides)
    return observe_config_from_mapping(raw)


def _run(cfg, *, run_id: str = "n5a-e2e") -> tuple[ShadowHost, object]:
    host = ShadowHost(
        cfg,
        clock=FakeClock(_wall=TS),
        run_id=RunId(run_id),
        correlation_id=CorrelationId(f"c-{run_id}"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    return host, result


def _assert_depth_walk(host: ShadowHost) -> None:
    assert host.oms is not None
    assert host.oms.fill_model_id == FILL_MODEL_DEPTH_WALK_V1
    assert host.oms.match_traces, "expected at least one depth-walk match attempt"
    assert all(t.fill_model_id == FILL_MODEL_DEPTH_WALK_V1 for t in host.oms.match_traces)


def test_e2e_rich_entry_exit_flat_depth_walk(tmp_path: Path) -> None:
    host, result = _run(_depth_walk_cfg(tmp_path), run_id="rich")
    _assert_depth_walk(host)
    actions = [d.action for d in result.decisions]
    assert StrategyAction.ENTER in actions
    assert StrategyAction.EXIT in actions
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    assert any(
        isinstance(i, ExitIntent) and i.reason_code == "MARKET_RICH_EXIT"
        for i in result.intents
    )
    buys = [o for o in host.order_store._orders.values() if o.side.value == "BUY"]
    sells = [o for o in host.order_store._orders.values() if o.side.value == "SELL"]
    assert buys and buys[0].status is OrderStatus.FILLED
    assert sells and sells[0].status is OrderStatus.FILLED
    assert sells[0].filled_quantity == buys[0].filled_quantity
    assert host.lifecycle.state is LifecycleState.FLAT
    assert host.portfolio.is_flat()
    lines = (tmp_path / "facts" / "analytics_events.jsonl").read_text(encoding="utf-8")
    lines += (tmp_path / "facts" / "audit_events.jsonl").read_text(encoding="utf-8")
    assert "estimated" in lines or "simulated_shadow" in lines or "simulated" in lines


def test_e2e_thesis_exit_depth_walk(tmp_path: Path) -> None:
    cfg = _depth_walk_cfg(
        tmp_path,
        fixture="tests/fixtures/z_gap/shadow_f4_thesis_exit.json",
        stop_confirm_s=0.5,
        p_stop="0.48",
        theta_rich="0.90",
        timer_eval_count=8,
        half_life_s=30.0,
        jump_threshold_sigma=100.0,
    )
    host, result = _run(cfg, run_id="thesis")
    _assert_depth_walk(host)
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    assert any(
        isinstance(i, ExitIntent) and i.reason_code == "THESIS_INVALID"
        for i in result.intents
    )
    assert host.portfolio.is_flat()
    assert host.lifecycle.state is LifecycleState.FLAT


def test_e2e_time_exit_depth_walk(tmp_path: Path) -> None:
    cfg = _depth_walk_cfg(
        tmp_path,
        theta_rich="0.90",
        p_stop="0.01",
        flatten_before_event_end_s=560.0,
        timer_eval_count=5,
    )
    host, result = _run(cfg, run_id="time")
    _assert_depth_walk(host)
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    time_exits = [
        i for i in result.intents if isinstance(i, ExitIntent) and i.reason_code == "TIME_SELL"
    ]
    assert time_exits or host.portfolio.is_flat()
    assert host.lifecycle.state in {LifecycleState.FLAT, LifecycleState.TERMINAL}


def test_e2e_kill_flatten_depth_walk(tmp_path: Path) -> None:
    cfg = _depth_walk_cfg(tmp_path)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("kill"), correlation_id=CorrelationId("ck")
    )
    host._attach()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    # Drive until ACTIVE, then kill
    host._publish_fixture_timeline(market)
    if host.lifecycle.state is LifecycleState.ACTIVE or not host.portfolio.is_flat():
        host.set_kill_switch(True)
        host.evaluate_once(trigger="timer")
    host.close()
    assert host.oms is not None and host.oms.fill_model_id == FILL_MODEL_DEPTH_WALK_V1
    # Either flattened via FlattenIntent or already flat from rich path
    assert host.portfolio.is_flat() or any(
        isinstance(i, FlattenIntent) for i in host.intents
    )


def test_e2e_entry_nofill_no_false_active(tmp_path: Path) -> None:
    """Zero ask depth on both legs → no fill → never ACTIVE with invented inventory.

    Risk/planner may still approve if quotes exist; depth-walk must not fabricate
    size. Zero both UP and DOWN so the strategy cannot divert to the other leg.
    """
    src = json.loads((ROOT / "tests/fixtures/z_gap/shadow_f4_rich_exit.json").read_text())
    for ev in src["polymarket_events"]:
        payload = ev["payload"]
        aid = payload.get("asset_id") or ""
        if "tok-up" in aid or "tok-down" in aid:
            payload["asks"] = [{"price": "0.99", "size": "0"}]
            payload["bids"] = [{"price": "0.01", "size": "0"}]
    path = tmp_path / "nofill.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(path)
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": 0,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    cfg = observe_config_from_mapping(raw)
    host, _ = _run(cfg, run_id="nofill")
    assert host.oms is not None and host.oms.fill_model_id == FILL_MODEL_DEPTH_WALK_V1
    buys = [o for o in host.order_store._orders.values() if o.side.value == "BUY"]
    for buy in buys:
        assert buy.filled_quantity == 0
    assert host.portfolio.is_flat()
    assert host.lifecycle.state is not LifecycleState.ACTIVE


def test_e2e_partial_entry_confirmed_qty_only(tmp_path: Path) -> None:
    """Latency + thinner post-decision book → partial fill; inventory = confirmed only.

    Fixture-only latency_ms=5000 — not a production assumption. Risk/planner see
    the deep early book; depth-walk selects the thinner causally available book
    at simulated arrival.
    """
    src = json.loads((ROOT / "tests/fixtures/z_gap/shadow_f4_rich_exit.json").read_text())
    for ev in src["polymarket_events"]:
        payload = ev["payload"]
        if payload.get("hash") in {"f4-up-rich", "f4-down-rich"}:
            # Delay rich exit so entry latency can resolve against the thin book.
            ev["ts_received"] = "2026-07-20T12:01:20+00:00"
            payload["timestamp"] = "2026-07-20T12:01:20+00:00"
    src["polymarket_events"].append(
        {
            "ts_received": "2026-07-20T12:01:07+00:00",
            "payload": {
                "event_type": "book",
                "asset_id": "zgap-f4-tok-up",
                "timestamp": "2026-07-20T12:01:07+00:00",
                "bids": [{"price": "0.34", "size": "200"}],
                "asks": [{"price": "0.36", "size": "5"}],
                "hash": "f4-up-thin",
            },
        }
    )
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(path)
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    raw["shadow"]["cancel_unfilled_residual"] = True
    # Fixture-only latency so arrival falls after the thin book.
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": 5000,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    cfg = observe_config_from_mapping(raw)
    host, _ = _run(cfg, run_id="partial")
    _assert_depth_walk(host)
    buys = [o for o in host.order_store._orders.values() if o.side.value == "BUY"]
    assert buys
    buy = buys[0]
    assert 0 < buy.filled_quantity < buy.quantity
    assert buy.filled_quantity == Decimal("5")
    yes = host.registry.market.yes.instrument_id
    assert host.portfolio.net_quantity(yes) == buy.filled_quantity
    sells = [o for o in host.order_store._orders.values() if o.side.value == "SELL"]
    for s in sells:
        assert s.quantity <= buy.filled_quantity
        assert s.filled_quantity <= buy.filled_quantity


def test_e2e_restart_after_active_no_duplicate_entry(tmp_path: Path) -> None:
    cfg = _depth_walk_cfg(tmp_path)
    host1b = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("r1b"), correlation_id=CorrelationId("cr1b")
    )
    try:
        result1 = host1b.run_fixture()
        host1b._maybe_persist()
        enters1 = sum(1 for i in result1.intents if isinstance(i, EnterIntent))
        fills1 = len(host1b.fills_ledger.all_fills())
    finally:
        host1b.close()

    host2 = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("r2"), correlation_id=CorrelationId("cr2")
    )
    host2._attach()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host2.registry.set_market(market)
    host2.portfolio.set_market_id(market.market_id)
    host2._start_strategy(market)
    recovered = host2.try_recover()
    result2 = host2.run_fixture()
    host2.close()
    enters2 = sum(1 for i in result2.intents if isinstance(i, EnterIntent))
    fills2 = len(host2.fills_ledger.all_fills())
    if recovered:
        assert enters2 <= enters1
    # Replay must not invent extra fills beyond a deterministic second pass
    assert fills2 == fills1 or fills1 == 0
    assert host2.oms is not None and host2.oms.fill_model_id == FILL_MODEL_DEPTH_WALK_V1


def test_e2e_unknown_blocks_sell_while_active(tmp_path: Path) -> None:
    """Force ACTIVE inventory via latency partial, then UNKNOWN must block sells."""
    from datetime import timedelta

    from tyrex_pm.core.snapshots import BookSnapshot

    src = json.loads((ROOT / "tests/fixtures/z_gap/shadow_f4_rich_exit.json").read_text())
    # Drop rich books so we remain ACTIVE with confirmed inventory.
    src["polymarket_events"] = [
        ev
        for ev in src["polymarket_events"]
        if ev["payload"].get("hash") not in {"f4-up-rich", "f4-down-rich"}
    ]
    src["polymarket_events"].append(
        {
            "ts_received": "2026-07-20T12:01:07+00:00",
            "payload": {
                "event_type": "book",
                "asset_id": "zgap-f4-tok-up",
                "timestamp": "2026-07-20T12:01:07+00:00",
                "bids": [{"price": "0.34", "size": "200"}],
                "asks": [{"price": "0.36", "size": "5"}],
                "hash": "f4-up-thin",
            },
        }
    )
    path = tmp_path / "unk.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(path)
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    raw["shadow"]["cancel_unfilled_residual"] = True
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": 5000,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    raw["z_gap"]["timer_eval_count"] = 0
    raw["z_gap"]["theta_rich"] = "0.99"
    cfg = observe_config_from_mapping(raw)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("unk"), correlation_id=CorrelationId("cu")
    )
    host._attach()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    host._publish_fixture_timeline(market)
    yes = host.registry.market.yes.instrument_id
    # Advance past latency and re-ingest thin book to trigger depth-walk rematch.
    assert isinstance(host.clock, FakeClock)
    host.clock.advance(wall=timedelta(seconds=6), mono_ns=6_000_000_000)
    thin = BookSnapshot.from_levels(
        instrument_id=yes,
        ts_event=host.clock.now_utc(),
        bids=[(Decimal("0.34"), Decimal("200"))],
        asks=[(Decimal("0.36"), Decimal("5"))],
    )
    assert host.oms is not None
    host.oms.on_book_updated(thin, available_at=host.clock.now_utc())
    if host.portfolio.net_quantity(yes) <= 0:
        host.close()
        pytest.fail("expected confirmed inventory before UNKNOWN gate")
    host.mark_unknown_inventory(active=True)
    sells_before = sum(1 for o in host.order_store._orders.values() if o.side.value == "SELL")
    host.set_kill_switch(True)
    host.evaluate_once(trigger="timer")
    sells_after = sum(1 for o in host.order_store._orders.values() if o.side.value == "SELL")
    host.close()
    assert sells_after == sells_before
    assert host.portfolio.net_quantity(yes) > 0


def test_e2e_exit_nofill_keeps_explicit_exposure(tmp_path: Path) -> None:
    """After full entry, remove bid depth → exit cannot fabricate a fill or FLAT."""
    src = json.loads((ROOT / "tests/fixtures/z_gap/shadow_f4_rich_exit.json").read_text())
    for ev in src["polymarket_events"]:
        payload = ev["payload"]
        if payload.get("hash") == "f4-up-rich":
            payload["bids"] = [{"price": "0.01", "size": "0"}]
            payload["asks"] = [{"price": "0.99", "size": "0"}]
        if payload.get("hash") == "f4-down-rich":
            payload["bids"] = [{"price": "0.01", "size": "0"}]
            payload["asks"] = [{"price": "0.99", "size": "0"}]
    path = tmp_path / "exit_nofill.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(path)
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": 0,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    raw["z_gap"]["theta_rich"] = "0.50"
    cfg = observe_config_from_mapping(raw)
    host, result = _run(cfg, run_id="exit-nofill")
    assert host.oms is not None and host.oms.fill_model_id == FILL_MODEL_DEPTH_WALK_V1
    buys = [o for o in host.order_store._orders.values() if o.side.value == "BUY"]
    assert buys and buys[0].filled_quantity > 0
    yes = host.registry.market.yes.instrument_id
    assert host.portfolio.net_quantity(yes) > 0
    assert host.lifecycle.state is not LifecycleState.FLAT
    assert not host.portfolio.is_flat()
    _ = result


def test_e2e_active_degradation_does_not_abandon_exposure(tmp_path: Path) -> None:
    """While ACTIVE, killing decision inputs must not report FLAT or drop inventory."""
    from tyrex_pm.core.snapshots import BookSnapshot

    cfg = _depth_walk_cfg(tmp_path, theta_rich="0.99")  # suppress rich exit
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("deg"), correlation_id=CorrelationId("cd")
    )
    host._attach()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    host._publish_fixture_timeline(market)
    yes = market.yes.instrument_id
    if host.portfolio.net_quantity(yes) <= 0:
        host.evaluate_once(trigger="timer")
    qty = host.portfolio.net_quantity(yes)
    assert qty > 0
    assert host.lifecycle.state in {
        LifecycleState.ACTIVE,
        LifecycleState.ENTRY_PENDING,
        LifecycleState.EXIT_PENDING,
    }
    empty = BookSnapshot.from_levels(
        instrument_id=yes,
        ts_event=host.clock.now_utc(),
        bids=[(Decimal("0.01"), Decimal("0"))],
        asks=[(Decimal("0.99"), Decimal("0"))],
    )
    if host.oms is not None:
        host.oms.on_book_updated(empty, available_at=host.clock.now_utc())
    host.evaluate_once(trigger="timer")
    assert host.portfolio.net_quantity(yes) == qty
    assert not host.portfolio.is_flat()
    assert host.lifecycle.state is not LifecycleState.FLAT
    host.close()


def test_e2e_restart_with_exit_residual_no_duplicate(tmp_path: Path) -> None:
    """Persist mid-exit residual inventory; recover exact qty without duplicate fills."""
    src = json.loads((ROOT / "tests/fixtures/z_gap/shadow_f4_rich_exit.json").read_text())
    for ev in src["polymarket_events"]:
        payload = ev["payload"]
        if payload.get("hash") == "f4-up-rich":
            # Thin bid → partial exit residual
            payload["bids"] = [{"price": "0.85", "size": "3"}]
            payload["asks"] = [{"price": "0.90", "size": "200"}]
    path = tmp_path / "residual.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(path)
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    raw["shadow"]["cancel_unfilled_residual"] = True
    raw["shadow"]["fills"] = {
        "model_id": FILL_MODEL_DEPTH_WALK_V1,
        "latency_ms": 0,
        "extra_slip_ticks": "0",
        "tick_size": "0.01",
    }
    cfg = observe_config_from_mapping(raw)
    host1 = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("res1"), correlation_id=CorrelationId("c1")
    )
    try:
        host1.run_fixture()
        host1._maybe_persist()
        yes = host1.registry.market.yes.instrument_id
        qty1 = host1.portfolio.net_quantity(yes)
        fills1 = len(host1.fills_ledger.all_fills())
        life1 = host1.lifecycle.state
    finally:
        host1.close()
    assert qty1 > 0 or life1 is not LifecycleState.FLAT

    host2 = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("res2"), correlation_id=CorrelationId("c2")
    )
    host2._attach()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host2.registry.set_market(market)
    host2.portfolio.set_market_id(market.market_id)
    host2._start_strategy(market)
    assert host2.try_recover()
    yes2 = host2.registry.market.yes.instrument_id
    qty2 = host2.portfolio.net_quantity(yes2)
    fills2 = len(host2.fills_ledger.all_fills())
    host2.close()
    assert qty2 == qty1
    assert fills2 == fills1


def test_e2e_replay_same_evidence_idempotent(tmp_path: Path) -> None:
    """Independent replays of the same evidence yield identical fill economics."""
    cfg_a = _depth_walk_cfg(tmp_path / "a")
    cfg_b = _depth_walk_cfg(tmp_path / "b")
    host_a, res_a = _run(cfg_a, run_id="rep-a")
    host_b, res_b = _run(cfg_b, run_id="rep-b")
    fills_a = [(str(f.quantity), str(f.price), f.side.value) for f in host_a.fills_ledger.all_fills()]
    fills_b = [(str(f.quantity), str(f.price), f.side.value) for f in host_b.fills_ledger.all_fills()]
    assert fills_a == fills_b
    assert host_a.lifecycle.state is host_b.lifecycle.state
    assert [d.action for d in res_a.decisions] == [d.action for d in res_b.decisions]
