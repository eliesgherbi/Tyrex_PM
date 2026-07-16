"""R2 architecture isolation checks."""

from __future__ import annotations

import ast
import pathlib

import tyrex_pm.core as core
import tyrex_pm.engine as engine


REPO = pathlib.Path(__file__).resolve().parents[1]


def _imports_in(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_core_and_engine_do_not_import_adapters_strategies_or_old() -> None:
    forbidden = {"old", "adapters", "strategies", "nautilus_trader", "nautilus"}
    for package_dir in (REPO / "src" / "tyrex_pm" / "core", REPO / "src" / "tyrex_pm" / "engine"):
        for path in package_dir.rglob("*.py"):
            imported = _imports_in(path)
            overlap = imported & forbidden
            assert not overlap, f"{path} imports {overlap}"


def test_core_does_not_import_engine() -> None:
    for path in (REPO / "src" / "tyrex_pm" / "core").rglob("*.py"):
        imported = _imports_in(path)
        assert "engine" not in imported
        assert "tyrex_pm.engine" not in {
            node.module
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom) and node.module
        }


def test_packages_importable() -> None:
    assert core.Event is not None
    assert engine.EventDispatcher is not None
