"""F2 architecture firewalls and F1 action-set regression."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm
from tyrex_pm.strategies.decisions import StrategyAction

ROOT = Path(tyrex_pm.__file__).resolve().parents[2]
SRC = ROOT / "src" / "tyrex_pm"


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


def _all_py_under(rel: str) -> list[Path]:
    base = SRC / rel
    return sorted(base.rglob("*.py"))


def test_f1_action_set_unchanged() -> None:
    assert {m.name for m in StrategyAction} == {
        "WAIT",
        "SKIP",
        "ENTER",
        "HOLD",
        "EXIT",
        "FLATTEN",
        "BLOCKED",
    }


def test_z_gap_has_no_forbidden_imports() -> None:
    forbidden_prefixes = (
        "old",
        "tyrex_pm.adapters",
        "tyrex_pm.risk",
        "tyrex_pm.planning",
        "tyrex_pm.execution",
        "tyrex_pm.portfolio",
        "tyrex_pm.persistence",
        "tyrex_pm.reporting",
        "tyrex_pm.runtime",
    )
    for path in _all_py_under("strategies/z_gap"):
        imports = _imports_of(path)
        for mod in imports:
            assert not (mod == "old" or mod.startswith("old.")), path
            for prefix in forbidden_prefixes:
                if prefix == "old":
                    continue
                assert not mod.startswith(prefix), f"{path} imports {mod}"


def test_indicators_have_no_zgap_thresholds_or_old() -> None:
    for path in _all_py_under("indicators"):
        text = path.read_text(encoding="utf-8")
        imports = _imports_of(path)
        assert not any(m == "old" or m.startswith("old.") for m in imports)
        assert "tyrex_pm.strategies.z_gap" not in imports
        # Indicators must not own Z-Gap entry thresholds
        assert "theta_take" not in text
        assert "theta_rich" not in text
        assert "ENTRY_CANDIDATE" not in text


def test_risk_planning_execution_do_not_import_z_gap() -> None:
    for rel in (
        "risk/engine.py",
        "risk/policies.py",
        "planning/planner.py",
        "planning/exit_planner.py",
        "execution/shadow_oms.py",
        "execution/protocol.py",
        "runtime/observe_host.py",
        "runtime/shadow_host.py",
    ):
        imports = _imports_of(SRC / rel)
        assert not any(m.startswith("tyrex_pm.strategies.z_gap") for m in imports), rel


def test_hosts_have_no_zgap_formulas() -> None:
    for rel in ("runtime/observe_host.py", "runtime/shadow_host.py"):
        text = (SRC / rel).read_text(encoding="utf-8")
        assert "market_richness" not in text
        assert "e_repricing" not in text
        assert "normal_cdf" not in text
        assert "EwmaVolatility" not in text


def test_no_zgap_strategy_orchestration_module() -> None:
    # F2 must not introduce host orchestration strategy.py
    assert not (SRC / "strategies" / "z_gap" / "strategy.py").exists()


def test_core_fees_phi_does_not_import_execution() -> None:
    imports = _imports_of(SRC / "core" / "fees_phi.py")
    assert not any(m.startswith("tyrex_pm.execution") for m in imports)
