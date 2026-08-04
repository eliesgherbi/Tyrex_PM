"""F3: Z-Gap fixture OBSERVE integration."""

from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import tyrex_pm
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, RunId
from tyrex_pm.runtime.observe_host import ObserveHost
from tyrex_pm.strategies.decisions import StrategyAction

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"
CFG = ROOT / "config" / "observe_z_gap_fixture_f3.json"
TS = datetime(2026, 7, 20, 12, 1, 0, tzinfo=timezone.utc)


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


def test_f3_precondition_fee_ownership() -> None:
    assert (SRC / "domain" / "polymarket" / "fees.py").exists()
    assert not (SRC / "core" / "fees_phi.py").exists()
    zgap_imports = set()
    for path in (SRC / "strategies" / "z_gap").rglob("*.py"):
        zgap_imports |= _imports_of(path)
    assert not any(m.startswith("tyrex_pm.execution") for m in zgap_imports)
    assert "tyrex_pm.domain.polymarket.fees" in zgap_imports or any(
        "fees" in m for m in zgap_imports
    )


def test_thin_strategy_has_no_formulas() -> None:
    text = (SRC / "strategies" / "z_gap" / "strategy.py").read_text(encoding="utf-8")
    assert "math.log" not in text
    assert "normal_cdf" not in text
    assert "ewma_lambda" not in text
    assert "phi_taker_fee_per_share" not in text
    imports = _imports_of(SRC / "strategies" / "z_gap" / "strategy.py")
    assert not any(m.startswith("tyrex_pm.adapters") for m in imports)
    assert not any(m.startswith("tyrex_pm.execution") for m in imports)
    assert not any(m.startswith("tyrex_pm.runtime") for m in imports)
    assert not any(m.startswith("tyrex_pm.risk") for m in imports)
    assert not any(m.startswith("tyrex_pm.planning") for m in imports)


def test_host_has_no_zgap_formulas_or_isinstance_strategy() -> None:
    text = (SRC / "runtime" / "observe_host.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "isinstance" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name) and arg.id in {"strategy", "ZGapStrategy"}:
                    raise AssertionError("host must not isinstance-check strategy")
    assert "math.log" not in text
    assert "normal_cdf" not in text
    assert "market_richness" not in text
    assert "e_repricing" not in text


def test_fixture_observe_zgap_deterministic_timeline(tmp_path: Path) -> None:
    from tyrex_pm.runtime.config import observe_config_from_mapping

    raw = json.loads(CFG.read_text(encoding="utf-8"))
    raw["output_path"] = str(tmp_path / "facts.jsonl")
    raw["fixture_path"] = str(ROOT / raw["fixture_path"])
    cfg = observe_config_from_mapping(raw)
    clock = FakeClock(_wall=TS)
    host = ObserveHost(
        cfg,
        clock=clock,
        run_id=RunId("run-f3"),
        correlation_id=CorrelationId("corr-f3"),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()

    actions = [d.action for d in result.decisions]
    assert StrategyAction.WAIT in actions or StrategyAction.SKIP in actions
    assert any(a is StrategyAction.ENTER for a in actions)
    assert len(result.intents) >= 1
    assert all(i.kind.value == "ENTER" for i in result.intents)

    # No OMS / fills / portfolio / realized PnL
    from helpers_reporting import (
        legacy_fact_types,
        load_events_for_legacy_jsonl,
        payloads_by_legacy_type,
    )

    events = load_events_for_legacy_jsonl(tmp_path, "facts.jsonl")
    types = legacy_fact_types(events)
    assert "intent_created" in types
    assert "intent_observe_no_oms" in types
    assert "zgap_model_snapshot" in types
    assert "zgap_entry_valuation" in types
    assert "zgap_calibration_row" in types
    assert "timer_elapsed" in types
    assert "order_submitted" not in types
    assert "fill" not in types
    assert "portfolio_update" not in types

    # Intent evidence is counterfactual/estimated
    intent_facts = payloads_by_legacy_type(events, "intent_created")
    assert intent_facts
    assert intent_facts[0]["oms_submit"] is False
    assert intent_facts[0]["observe_only"] is True

    calib = payloads_by_legacy_type(events, "zgap_calibration_row")
    assert calib
    assert calib[0]["schema"] == "z_gap_calibration_v1"
    assert calib[0]["valuation_label"] == "counterfactual"
    assert calib[0]["fee_label"] == "estimated"

    assert result.run_dir is not None
    assert result.summary_path is not None
    assert result.summary_path.is_file()
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["performance"]["label"] == "observed_only"

    # One entry lineage — later decisions should not storm more enters
    enter_count = sum(1 for a in actions if a is StrategyAction.ENTER)
    assert enter_count == 1
    assert len(result.intents) == 1

    # Plans/risk empty on observe-only path
    assert result.plans == []
    assert result.risk_decisions == []


def test_reporting_does_not_import_valuations() -> None:
    reporting = SRC / "reporting"
    for path in reporting.rglob("*.py"):
        imports = _imports_of(path)
        assert not any(m.startswith("tyrex_pm.strategies.z_gap") for m in imports)


def test_f1_actions_unchanged() -> None:
    assert {m.name for m in StrategyAction} == {
        "WAIT",
        "SKIP",
        "ENTER",
        "HOLD",
        "EXIT",
        "FLATTEN",
        "BLOCKED",
    }


def test_reference_momentum_fixture_still_works(tmp_path: Path) -> None:
    from tyrex_pm.runtime.config import observe_config_from_mapping

    raw = {
        "mode": "fixture",
        "strategy_kind": "reference_momentum",
        "fixture_path": str(ROOT / "tests/fixtures/r3_observe_complete.json"),
        "output_path": str(tmp_path / "rm.jsonl"),
        "binance_symbol": "BTCUSDT",
        "momentum_lookback_ms": 5000,
        "momentum_threshold": "0.001",
        "max_book_spread": "0.10",
        "freshness": {
            "book_threshold_ms": 60000,
            "reference_threshold_ms": 60000,
            "future_tolerance_ms": 500,
            "timestamp_basis": "EVENT_TIME",
        },
    }
    cfg = observe_config_from_mapping(raw)
    host = ObserveHost(
        cfg,
        clock=FakeClock(_wall=datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)),
    )
    try:
        result = host.run_fixture()
    finally:
        host.close()
    assert result.decisions
    assert host.config.strategy_kind == "reference_momentum"
