"""R4 architecture: no OMS/orders/fills/portfolio; no old/nautilus."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm

PKG = Path(tyrex_pm.__file__).resolve().parent


def test_no_oms_order_fill_portfolio_packages() -> None:
    names = {p.name for p in PKG.rglob("*") if p.is_dir()}
    for banned in ("oms", "portfolio", "orders", "fills"):
        assert banned not in names


def test_no_old_nautilus_imports() -> None:
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("old", "nautilus"))
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("old", "nautilus"))


def test_strategy_does_not_import_risk_or_adapters() -> None:
    strat = (
        PKG / "strategies" / "framework_validation" / "reference_momentum.py"
    ).read_text(encoding="utf-8")
    assert "tyrex_pm.risk" not in strat
    assert "tyrex_pm.adapters" not in strat
    assert "tyrex_pm.planning" not in strat


def test_exit_intents_not_implemented() -> None:
    text = (PKG / "core" / "intents.py").read_text(encoding="utf-8")
    assert "class ExitIntent" not in text
    assert "class FlattenIntent" not in text
    assert "class CancelIntent" not in text
