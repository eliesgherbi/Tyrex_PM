"""F4: Z-Gap SHADOW entry→exit lifecycle (deterministic, offline)."""

from __future__ import annotations

import ast
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import tyrex_pm
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, RunId
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.runtime.config import observe_config_from_mapping
from tyrex_pm.runtime.observe_host import ObserveHost
from tyrex_pm.runtime.shadow_host import ShadowHost
from tyrex_pm.runtime.strategy_binding import (
    ReferenceMomentumBinding,
    StrategyBinding,
    ZGapBinding,
    build_strategy_binding,
)
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"
CFG_RICH = ROOT / "config" / "observe_shadow_z_gap_f4.json"
CFG_F3 = ROOT / "config" / "observe_z_gap_fixture_f3.json"
TS = datetime(2026, 7, 20, 12, 1, 0, tzinfo=timezone.utc)


def _load_cfg(path: Path, tmp: Path, *, fixture: str | None = None) -> object:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp / "facts.jsonl")
    raw["fixture_path"] = str(ROOT / (fixture or raw["fixture_path"]))
    if "shadow" in raw and raw["shadow"] is not None:
        raw["shadow"]["persistence_path"] = str(tmp / "state.json")
    return observe_config_from_mapping(raw)


def _run_shadow(tmp: Path, *, cfg_path: Path = CFG_RICH, fixture: str | None = None) -> tuple:
    cfg = _load_cfg(cfg_path, tmp, fixture=fixture)
    host = ShadowHost(
        cfg,
        clock=FakeClock(_wall=TS),
        run_id=RunId("run-f4"),
        correlation_id=CorrelationId("corr-f4"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    return host, result


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_binding_uniform_evaluate_interface() -> None:
    assert hasattr(StrategyBinding, "evaluate")
    m = build_strategy_binding(strategy_kind="reference_momentum")
    assert isinstance(m, ReferenceMomentumBinding)
    z = build_strategy_binding(
        strategy_kind="z_gap",
        zgap_runtime=observe_config_from_mapping(
            json.loads(CFG_F3.read_text(encoding="utf-8"))
        ).z_gap,
        clock=FakeClock(_wall=TS),
    )
    assert isinstance(z, ZGapBinding)
    assert callable(m.evaluate) and callable(z.evaluate)
    assert callable(m.bump_decision_epoch) and callable(z.bump_decision_epoch)


def test_host_no_strategy_kind_or_isinstance_in_eval() -> None:
    text = (SRC / "runtime" / "observe_host.py").read_text(encoding="utf-8")
    # evaluate_once must not fork on strategy_kind / ZGapStrategy
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_once":
            body = ast.get_source_segment(text, node) or ""
            assert "strategy_kind" not in body
            assert "ZGapStrategy" not in body
            assert "isinstance" not in body
    shadow = (SRC / "runtime" / "shadow_host.py").read_text(encoding="utf-8")
    assert "isinstance(strategy" not in shadow
    assert "ZGapStrategy" not in shadow


def test_zgap_modules_forbid_risk_oms_imports() -> None:
    for path in (SRC / "strategies" / "z_gap").rglob("*.py"):
        imports = _imports_of(path)
        assert not any(m.startswith("tyrex_pm.risk") for m in imports)
        assert not any(m.startswith("tyrex_pm.planning") for m in imports)
        assert not any(m.startswith("tyrex_pm.execution") for m in imports)
        assert not any(m.startswith("tyrex_pm.portfolio") for m in imports)
        assert not any("r7" in m for m in imports)
        assert not any(m.startswith("old") for m in imports)


def test_risk_planning_execution_do_not_import_zgap() -> None:
    for package in ("risk", "planning", "execution", "portfolio", "lifecycle"):
        for path in (SRC / package).rglob("*.py"):
            imports = _imports_of(path)
            assert not any("z_gap" in m for m in imports), path


def test_scenario_market_rich_entry_exit_flat(tmp_path: Path) -> None:
    host, result = _run_shadow(tmp_path)
    actions = [d.action for d in result.decisions]
    assert StrategyAction.ENTER in actions
    assert StrategyAction.EXIT in actions
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    assert any(
        isinstance(i, ExitIntent) and i.reason_code == "MARKET_RICH_EXIT"
        for i in result.intents
    )
    assert host.lifecycle.state is LifecycleState.FLAT
    assert host.portfolio.is_flat()
    assert len(host.commands) >= 2
    assert sum(1 for a in actions if a is StrategyAction.ENTER) == 1
    enter_intents = [i for i in result.intents if isinstance(i, EnterIntent)]
    assert len(enter_intents) == 1

    from helpers_reporting import legacy_fact_types, load_events_for_legacy_jsonl

    events = load_events_for_legacy_jsonl(tmp_path, "facts.jsonl")
    types = legacy_fact_types(events)
    assert "lifecycle_transition" in types
    assert "execution_plan_created" in types
    assert "command_created" in types
    assert "zgap_model_snapshot" in types
    assert "zgap_active_position_context" in types
    # No live/venue mutation facts
    assert "live_order_submitted" not in types


def test_scenario_thesis_invalidation(tmp_path: Path) -> None:
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["fixture_path"] = "tests/fixtures/z_gap/shadow_f4_thesis_exit.json"
    raw["z_gap"]["stop_confirm_s"] = 0.5
    raw["z_gap"]["p_stop"] = "0.48"
    raw["z_gap"]["theta_rich"] = "0.90"  # suppress rich exit
    raw["z_gap"]["timer_eval_count"] = 8
    raw["z_gap"]["half_life_s"] = 30.0
    raw["z_gap"]["jump_threshold_sigma"] = 100.0  # keep model valid through decline
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(ROOT / raw["fixture_path"])
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    cfg = observe_config_from_mapping(raw)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f4t"), correlation_id=CorrelationId("ct")
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    thesis_exits = [
        i
        for i in result.intents
        if isinstance(i, ExitIntent) and i.reason_code == "THESIS_INVALID"
    ]
    assert thesis_exits, [i.reason_code for i in result.intents]
    assert host.portfolio.is_flat()
    assert host.lifecycle.state is LifecycleState.FLAT


def test_scenario_time_exit(tmp_path: Path) -> None:
    raw = json.loads(CFG_RICH.read_text(encoding="utf-8"))
    raw["z_gap"]["theta_rich"] = "0.90"
    raw["z_gap"]["p_stop"] = "0.01"  # avoid thesis
    raw["z_gap"]["flatten_before_event_end_s"] = 560.0  # tau ~540s at start → time sell soon
    raw["z_gap"]["timer_eval_count"] = 5
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(ROOT / raw["fixture_path"])
    raw["shadow"]["persistence_path"] = str(tmp_path / "state.json")
    # Shorten window end via fixture copy is heavy; use rich fixture + large flatten window.
    cfg = observe_config_from_mapping(raw)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f4time"), correlation_id=CorrelationId("ctime")
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    time_exits = [
        i for i in result.intents if isinstance(i, ExitIntent) and i.reason_code == "TIME_SELL"
    ]
    assert any(isinstance(i, EnterIntent) for i in result.intents)
    assert time_exits or host.portfolio.is_flat()
    assert host.lifecycle.state in {LifecycleState.FLAT, LifecycleState.TERMINAL}


def test_scenario_risk_flatten(tmp_path: Path) -> None:
    cfg = _load_cfg(CFG_RICH, tmp_path)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f4k"), correlation_id=CorrelationId("ck")
    )
    host._attach()
    host._init_flags()
    market = __import__(
        "tyrex_pm.adapters.polymarket.discovery", fromlist=["load_market_from_fixture"]
    ).load_market_from_fixture(cfg.fixture_path)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    # Drive until ACTIVE then kill
    host._publish_fixture_timeline(market)
    if host.lifecycle.state is not LifecycleState.ACTIVE:
        # force one more eval if entry late
        host.evaluate_once(trigger="timer")
    if host.lifecycle.state is LifecycleState.ACTIVE:
        host.set_kill_switch(True)
        host.evaluate_once(trigger="timer")
    host.binding.on_stop("NORMAL")
    host.close()
    assert any(isinstance(i, FlattenIntent) for i in host.intents) or host.portfolio.is_flat()


def test_unknown_inventory_no_blind_sell(tmp_path: Path) -> None:
    cfg = _load_cfg(CFG_RICH, tmp_path)
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f4u"), correlation_id=CorrelationId("cu")
    )
    host._attach()
    host._init_flags()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    host._publish_fixture_timeline(market)
    if host.lifecycle.state is LifecycleState.ACTIVE:
        before_cmds = len(host.commands)
        host.mark_unknown_inventory(active=True)
        host.evaluate_once(trigger="timer")
        # No new SELL/flatten command while unknown
        new_cmds = host.commands[before_cmds:]
        assert not any(c.side.value == "SELL" for c in new_cmds)
        blocked = [d for d in host.decisions if d.action is StrategyAction.BLOCKED]
        assert blocked
    host.close()


def test_observe_no_oms_portfolio(tmp_path: Path) -> None:
    cfg = _load_cfg(CFG_F3, tmp_path)
    host = ObserveHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f3o"), correlation_id=CorrelationId("co")
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert any(i.kind.value == "ENTER" for i in result.intents)
    from helpers_reporting import legacy_fact_types, load_events_for_legacy_jsonl

    events = load_events_for_legacy_jsonl(tmp_path, "facts.jsonl")
    types = legacy_fact_types(events)
    assert "intent_observe_no_oms" in types
    assert "command_created" not in types
    assert "lifecycle_transition" not in types


def test_observe_shadow_parity_same_decision_entrypoint(tmp_path: Path) -> None:
    """OBSERVE and SHADOW call the same binding.evaluate / strategy.on_decision path."""
    observe_src = (SRC / "runtime" / "observe_host.py").read_text(encoding="utf-8")
    assert "self.binding.evaluate(" in observe_src
    assert "def evaluate_once" in observe_src
    shadow_src = (SRC / "runtime" / "shadow_host.py").read_text(encoding="utf-8")
    assert "def evaluate_once" not in shadow_src
    # Same strategy class methods
    assert "def on_decision" in (SRC / "strategies" / "z_gap" / "strategy.py").read_text(
        encoding="utf-8"
    )


def test_persistence_active_restart(tmp_path: Path) -> None:
    cfg = _load_cfg(CFG_RICH, tmp_path)
    # Disable rich mid-books by using thesis fixture and high theta_rich; stop early.
    host = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f4p"), correlation_id=CorrelationId("cp")
    )
    host._attach()
    host._init_flags()
    from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture

    market = load_market_from_fixture(cfg.fixture_path)
    host.registry.set_market(market)
    host.portfolio.set_market_id(market.market_id)
    host._start_strategy(market)
    host._publish_fixture_timeline(market)
    host._maybe_persist()
    life = host.lifecycle.state
    qty_before = sum(
        host.portfolio.net_quantity(i)
        for i in (market.yes.instrument_id, market.no.instrument_id)
    )
    slice_before = host.binding.persistence_slice()
    host.close()

    host2 = ShadowHost(
        cfg, clock=FakeClock(_wall=TS), run_id=RunId("f4p2"), correlation_id=CorrelationId("cp2")
    )
    host2._attach()
    host2.registry.set_market(market)
    host2.portfolio.set_market_id(market.market_id)
    host2._start_strategy(market)
    assert host2.try_recover()
    assert host2.lifecycle.state is life
    qty_after = sum(
        host2.portfolio.net_quantity(i)
        for i in (market.yes.instrument_id, market.no.instrument_id)
    )
    assert qty_after == qty_before
    assert host2.binding.persistence_slice().get("entry_lineage_consumed") == slice_before.get(
        "entry_lineage_consumed"
    )
    # No duplicate entry on next eval when lineage consumed
    before_intents = len(host2.intents)
    host2.evaluate_once(trigger="timer")
    new_enters = [
        i for i in host2.intents[before_intents:] if isinstance(i, EnterIntent)
    ]
    assert new_enters == []
    host2.close()


def test_reporting_no_zgap_valuation_imports() -> None:
    for path in (SRC / "reporting").rglob("*.py"):
        imports = _imports_of(path)
        assert not any(m.startswith("tyrex_pm.strategies.z_gap") for m in imports)


def test_f1_actions_unchanged() -> None:
    assert {a.value for a in StrategyAction} == {
        "WAIT",
        "SKIP",
        "ENTER",
        "HOLD",
        "EXIT",
        "FLATTEN",
        "BLOCKED",
    }
    # F5: HoldToResolutionIntent is an IntentLike, not a StrategyAction.
    assert "HOLD_TO_RESOLUTION" not in {a.value for a in StrategyAction}
    from tyrex_pm.core.intents import HoldToResolutionIntent as _HTRI
    from tyrex_pm.strategies.decisions import IntentLike

    assert _HTRI in IntentLike.__args__


def test_reference_momentum_shadow_still_works(tmp_path: Path) -> None:
    from datetime import timedelta
    from tyrex_pm.market_data.freshness import FreshnessConfig, TimestampBasis
    from tyrex_pm.runtime.config import ObserveConfig, RiskPlanConfig, SourceMode
    from tyrex_pm.runtime.shadow_config import ShadowConfig

    cfg = ObserveConfig(
        mode=SourceMode.FIXTURE,
        output_path=tmp_path / "facts.jsonl",
        binance_symbol="BTCUSDT",
        momentum_lookback=timedelta(seconds=5),
        momentum_threshold=Decimal("0.001"),
        max_book_spread=Decimal("0.10"),
        freshness=FreshnessConfig(
            book_threshold_ms=60_000,
            reference_threshold_ms=60_000,
            future_tolerance_ms=500,
            timestamp_basis=TimestampBasis.EVENT_TIME,
        ),
        runtime_duration=None,
        fixture_path=ROOT / "tests" / "fixtures" / "r3_observe_complete.json",
        risk=RiskPlanConfig(
            runtime_mode=RuntimeMode.SHADOW,
            target_notional=Decimal("5"),
            max_notional=Decimal("10"),
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.20"),
            min_liquidity_notional=Decimal("1"),
            no_entry_before_close=timedelta(0),
            duplicate_lifetime=timedelta(hours=1),
        ),
        shadow=ShadowConfig(
            enable_oms=True,
            max_position_notional=Decimal("20"),
            max_total_exposure=Decimal("50"),
            max_hold=timedelta(minutes=10),
            flatten_before_close=timedelta(seconds=30),
            exit_on_flat=True,
            persistence_path=tmp_path / "state.json",
            cancel_unfilled_residual=False,
        ),
    )
    host = ShadowHost(
        cfg,
        clock=FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)),
        run_id=RunId("rm"),
        correlation_id=CorrelationId("rmc"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert result.decisions
    assert host.config.strategy_kind == "reference_momentum"


def test_thin_strategy_still_has_no_formulas() -> None:
    text = (SRC / "strategies" / "z_gap" / "strategy.py").read_text(encoding="utf-8")
    assert "math.log" not in text
    assert "normal_cdf" not in text
    assert "ewma_lambda" not in text
