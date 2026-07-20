"""F1: neutral StrategyDecision / IntentLike / protocol architecture."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import tyrex_pm
from tyrex_pm.core.ids import CorrelationId, StrategyId
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, HoldToResolutionIntent
from tyrex_pm.strategies.decisions import IntentLike, StrategyAction, StrategyDecision

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"
TS = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _imports_of(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_f1_action_vocabulary_exact() -> None:
    names = {m.name for m in StrategyAction}
    assert names == {"WAIT", "SKIP", "ENTER", "HOLD", "EXIT", "FLATTEN", "BLOCKED"}
    assert "STOP" not in names
    assert "HOLD_TO_RESOLUTION" not in names


def test_reference_momentum_validation_kind_maps_to_neutral_actions() -> None:
    from decimal import Decimal

    from tyrex_pm.core.ids import CorrelationId, EventId
    from tyrex_pm.signals.directional import Direction, DirectionalSignal
    from tyrex_pm.strategies.framework_validation.reference_momentum import (
        ObserveDecisionKind,
        ReferenceMomentumStrategy,
    )

    strat = ReferenceMomentumStrategy()
    cid = CorrelationId("map")
    eid = EventId("e1")

    def _sig(direction: Direction) -> DirectionalSignal:
        return DirectionalSignal(
            direction=direction,
            observed_at=TS,
            correlation_id=cid,
            causation_id=eid,
            momentum=None,
            threshold=Decimal("0.001"),
            strength=None,
            selected_outcome=None,
            reason_code="TEST",
            evidence={},
        )

    up = strat.evaluate(_sig(Direction.UP))
    assert up.action is StrategyAction.ENTER
    assert up.evidence["validation_kind"] == ObserveDecisionKind.WOULD_ENTER_UP.value

    down = strat.evaluate(_sig(Direction.DOWN))
    assert down.action is StrategyAction.ENTER
    assert down.evidence["validation_kind"] == ObserveDecisionKind.WOULD_ENTER_DOWN.value

    flat = strat.evaluate(_sig(Direction.FLAT))
    assert flat.action is StrategyAction.HOLD
    assert flat.evidence["validation_kind"] == ObserveDecisionKind.HOLD.value

    skip = strat.evaluate(_sig(Direction.UNAVAILABLE))
    assert skip.action is StrategyAction.SKIP
    assert skip.evidence["validation_kind"] == ObserveDecisionKind.SKIP.value


def test_strategy_decision_has_no_validation_dependency() -> None:
    imports = _imports_of(SRC / "strategies" / "decisions.py")
    assert not any("framework_validation" in m for m in imports)
    assert not any(m.startswith("tyrex_pm.adapters") for m in imports)
    assert not any("execution.polymarket" in m for m in imports)
    assert not any(m.startswith("tyrex_pm.runtime.r7") for m in imports)


def test_protocol_does_not_import_framework_validation() -> None:
    imports = _imports_of(SRC / "strategies" / "protocol.py")
    assert not any("framework_validation" in m for m in imports)
    # Protocol returns StrategyDecision + IntentLike
    text = (SRC / "strategies" / "protocol.py").read_text(encoding="utf-8")
    assert "StrategyDecision" in text
    assert "IntentLike" in text
    assert "ObserveDecision" not in text
    assert "EnterIntent" not in text or "IntentLike" in text


def test_intent_like_accepts_enter_exit_flatten_and_hold_to_resolution() -> None:
    # F5 adds HoldToResolutionIntent; F1 StrategyAction vocabulary stays unchanged.
    assert IntentLike == EnterIntent | ExitIntent | FlattenIntent | HoldToResolutionIntent
    assert "HOLD_TO_RESOLUTION" not in {a.value for a in StrategyAction}


def test_strategy_decision_minimal_fields() -> None:
    d = StrategyDecision(
        action=StrategyAction.ENTER,
        reason_code="TEST",
        decided_at=TS,
        correlation_id=CorrelationId("c1"),
        strategy_id=StrategyId("s"),
        evidence={"validation_kind": "WOULD_ENTER_UP"},
    )
    assert d.action is StrategyAction.ENTER
    assert d.decision_id
    assert "order" not in d.evidence
    assert "FAK" not in str(d.evidence)


def test_risk_planning_execution_do_not_import_concrete_strategies() -> None:
    forbidden_prefixes = (
        "tyrex_pm.strategies.framework_validation",
        "tyrex_pm.strategies.z_gap",
    )
    for rel in (
        "risk/engine.py",
        "risk/policies.py",
        "planning/planner.py",
        "planning/exit_planner.py",
        "execution/shadow_oms.py",
        "execution/protocol.py",
    ):
        imports = _imports_of(SRC / rel)
        for mod in imports:
            for prefix in forbidden_prefixes:
                assert not mod.startswith(prefix), f"{rel} imports {mod}"


def test_no_old_or_r7_in_strategy_contracts() -> None:
    for rel in ("strategies/decisions.py", "strategies/protocol.py"):
        imports = _imports_of(SRC / rel)
        assert not any(m == "old" or m.startswith("old.") for m in imports)
        assert not any(m.startswith("tyrex_pm.runtime.r7") for m in imports)


def test_reference_momentum_satisfies_strategy_protocol() -> None:
    from tyrex_pm.strategies.framework_validation.reference_momentum import (
        ReferenceMomentumStrategy,
    )

    assert callable(ReferenceMomentumStrategy.on_start)
    assert callable(ReferenceMomentumStrategy.on_signal)
    assert callable(ReferenceMomentumStrategy.on_stop)


def test_docs_consistency_protocol_callbacks_still_hold() -> None:
    path = SRC / "strategies" / "protocol.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    methods = {
        n.name
        for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "Strategy"
        for n in n.body
        if isinstance(n, ast.FunctionDef)
    }
    assert methods == {"on_start", "on_signal", "on_stop"}
