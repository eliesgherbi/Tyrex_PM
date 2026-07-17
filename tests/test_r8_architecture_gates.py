"""R8 architecture gates: strategy boundaries, no old/, no default network."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm

PKG = Path(tyrex_pm.__file__).resolve().parent
ROOT = PKG.parents[1]


FORBIDDEN_STRATEGY_IMPORT_PREFIXES = (
    "tyrex_pm.execution.polymarket",
    "tyrex_pm.execution.shadow_oms",
    "tyrex_pm.execution.live_oms",
    "tyrex_pm.portfolio",
    "tyrex_pm.persistence",
)


def _imports_of(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def test_strategies_do_not_import_execution_portfolio_persistence() -> None:
    strat_root = PKG / "strategies"
    offenders: list[str] = []
    for path in strat_root.rglob("*.py"):
        for mod in _imports_of(path):
            if any(mod == p or mod.startswith(p + ".") for p in FORBIDDEN_STRATEGY_IMPORT_PREFIXES):
                offenders.append(f"{path.relative_to(PKG)}:{mod}")
    assert offenders == []


def test_no_old_imports_anywhere_in_src() -> None:
    for path in PKG.rglob("*.py"):
        for mod in _imports_of(path):
            assert mod != "old" and not mod.startswith("old."), path
        text = path.read_text(encoding="utf-8")
        assert "from old " not in text
        assert "from old." not in text
        assert "import old " not in text
        assert "import old\n" not in text
        assert "import old\r" not in text


def test_default_tests_do_not_call_network_helpers_directly() -> None:
    """Guard: unit tests must not hardcode live CLOB hosts in assert paths."""
    # Construct hosts so this file does not contain the banned literals itself.
    banned = (
        "https://" + "clob.polymarket.com",
        "https://" + "data-api.polymarket.com",
        "wss://" + "ws-subscriptions-clob.polymarket.com",
    )
    # scripts/ may call network; tests/ must stay offline by default.
    allow = {
        "test_r6b_readonly_live_optional.py",  # optional live; skipped without flag
        "test_r8_architecture_gates.py",
    }
    offenders: list[str] = []
    for path in (ROOT / "tests").rglob("*.py"):
        if path.name in allow:
            continue
        text = path.read_text(encoding="utf-8")
        for b in banned:
            if b in text:
                offenders.append(f"{path.name}:{b}")
    assert offenders == []


def test_reference_momentum_is_framework_validation_only() -> None:
    text = (PKG / "strategies" / "framework_validation" / "reference_momentum.py").read_text(
        encoding="utf-8"
    )
    # Must remain a thin validation strategy, not a Z-Gap port.
    assert "z_gap" not in text.lower()
    assert "ZGap" not in text
