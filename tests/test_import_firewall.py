"""Ensure the active package is isolated from the archived tree."""

from __future__ import annotations

import importlib
import pathlib
import sys

import tyrex_pm


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OLD_ROOT = REPO_ROOT / "old"
NEW_SRC = REPO_ROOT / "src" / "tyrex_pm"


def test_old_directory_exists_as_reference() -> None:
    assert OLD_ROOT.is_dir()
    assert (OLD_ROOT / "src" / "tyrex_pm").is_dir()


def test_installed_tyrex_pm_comes_from_new_src() -> None:
    package_path = pathlib.Path(tyrex_pm.__file__).resolve()
    assert NEW_SRC in package_path.parents or package_path.parent == NEW_SRC
    assert "old" not in package_path.parts


def test_active_modules_do_not_import_old() -> None:
    # Fresh import of the public surface.
    mod = importlib.import_module("tyrex_pm.application.cli")
    for name, module in list(sys.modules.items()):
        if module is None:
            continue
        if name == "old" or name.startswith("old."):
            raise AssertionError(f"forbidden module loaded: {name}")
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = pathlib.Path(file).resolve()
        try:
            path.relative_to(OLD_ROOT)
        except ValueError:
            continue
        # Allow nothing under old/ to be imported.
        raise AssertionError(f"module {name} loaded from old/: {path}")

    assert mod is not None


def test_old_tests_are_not_on_pytest_path(pytestconfig) -> None:
    testpaths = pytestconfig.getini("testpaths")
    assert testpaths == ["tests"]
    for tp in testpaths:
        resolved = (REPO_ROOT / tp).resolve()
        assert resolved == (REPO_ROOT / "tests").resolve()
        assert not str(resolved).startswith(str(OLD_ROOT))


def test_sys_path_does_not_prefer_old_src() -> None:
    old_src = str((OLD_ROOT / "src").resolve())
    for entry in sys.path:
        if not entry:
            continue
        try:
            resolved = str(pathlib.Path(entry).resolve())
        except OSError:
            continue
        assert resolved != old_src, "old/src must not be on sys.path"
