"""R3 architecture dependency and package rules."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm

ROOT = Path(tyrex_pm.__file__).resolve().parent
PKG_ROOT = ROOT


FORBIDDEN_IMPORT_PREFIXES = (
    "old",
    "nautilus_trader",
    "nautilus",
)

FORBIDDEN_NAME_FRAGMENTS = (
    "OrderManager",
    "PortfolioStore",
    "FillEvent",
    "OrderSubmitted",
)


def _python_files() -> list[Path]:
    return [p for p in PKG_ROOT.rglob("*.py") if p.is_file()]


def test_no_old_or_nautilus_imports() -> None:
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    assert not name.startswith(FORBIDDEN_IMPORT_PREFIXES), path
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    assert not node.module.startswith(FORBIDDEN_IMPORT_PREFIXES), path


def test_no_risk_oms_portfolio_modules() -> None:
    names = {p.name for p in _python_files()}
    for banned in ("risk.py", "oms.py", "portfolio.py", "orders.py", "fills.py"):
        assert banned not in names


def test_no_forbidden_symbols_in_r3_surface() -> None:
    text_blobs = [p.read_text(encoding="utf-8") for p in _python_files()]
    joined = "\n".join(text_blobs)
    for frag in FORBIDDEN_NAME_FRAGMENTS:
        assert frag not in joined


def test_distribution_excludes_old() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert 'exclude = ["old*' in text or "exclude = [\"old*" in text
    assert "nautilus" not in text.lower()


def test_snapshot_consistency_fields() -> None:
    from dataclasses import fields

    from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot

    names = {f.name for f in fields(DecisionSnapshot)}
    assert "yes_book" in names and "reference" in names
    assert "intent" not in names
    assert "portfolio" not in names
