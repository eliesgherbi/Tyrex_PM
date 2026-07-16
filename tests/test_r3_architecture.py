"""R3 architecture dependency and package rules (still enforced in R5+)."""

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
