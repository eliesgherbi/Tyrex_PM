"""Unit tests for guru run file logging helpers (`tyrex_pm.runtime.guru_run_logging`)."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from tyrex_pm.runtime import guru_run_logging as grl


def test_resolve_tyrex_default_shadow(tmp_path: Path) -> None:
    p = grl.resolve_guru_source_log_path(tmp_path, "shadow", None, "tyrex")
    assert p.name == "run_tyrex.log"
    assert p.parent.name == "shadow"


def test_resolve_nautilus_default_live(tmp_path: Path) -> None:
    p = grl.resolve_guru_source_log_path(tmp_path, "live", None, "nautilus")
    assert p.name == "run_nautilus.log"
    assert p.parent.name == "live"


def test_resolve_blank_execution_mode_uses_live_folder(tmp_path: Path) -> None:
    p = grl.resolve_guru_source_log_path(tmp_path, "", None, "tyrex")
    assert p.parent.name == "live"


def test_resolve_log_name_both_sources(tmp_path: Path) -> None:
    t = grl.resolve_guru_source_log_path(tmp_path, "shadow", "my-session", "tyrex")
    n = grl.resolve_guru_source_log_path(tmp_path, "shadow", "my-session", "nautilus")
    assert t.name == "my-session_tyrex.log"
    assert n.name == "my-session_nautilus.log"
    assert t.parent == n.parent


def test_sanitize_default_and_strip() -> None:
    assert grl.sanitize_log_name(None) == "run"
    assert grl.sanitize_log_name("") == "run"
    assert grl.sanitize_log_name("  ") == "run"


def test_sanitize_rejects_path_traversal_and_separators() -> None:
    for bad in ("../x", "..", "a/b", "a\\b", "a:b", "COM1", "com1.txt"):
        with pytest.raises(ValueError):
            grl.sanitize_log_name(bad)


def test_sanitize_rejects_other_unsafe() -> None:
    with pytest.raises(ValueError):
        grl.sanitize_log_name("my name")
    with pytest.raises(ValueError):
        grl.sanitize_log_name("-bad")
    with pytest.raises(ValueError):
        grl.sanitize_log_name("a" * 101)


def test_ensure_guru_run_log_dir_creates_parents(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "live" / "run_tyrex.log"
    grl.ensure_guru_run_log_dir(log_file)
    assert log_file.parent.is_dir()


def test_attach_tyrex_pm_file_handler_no_duplicate_same_path(tmp_path: Path) -> None:
    log_file = tmp_path / "run_tyrex.log"
    tyrex = logging.getLogger("tyrex_pm")
    saved = list(tyrex.handlers)
    tyrex.handlers.clear()
    try:
        h1 = grl.attach_tyrex_pm_file_handler(log_file)
        assert h1 is not None
        assert len(tyrex.handlers) == 1
        h2 = grl.attach_tyrex_pm_file_handler(log_file)
        assert h2 is None
        assert len(tyrex.handlers) == 1
    finally:
        for h in list(tyrex.handlers):
            tyrex.removeHandler(h)
            h.close()
        for h in saved:
            tyrex.addHandler(h)


def test_announce_guru_run_log_destinations_both_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ty = tmp_path / "logs" / "live" / "run_tyrex.log"
    na = tmp_path / "logs" / "live" / "run_nautilus.log"
    t_line, n_line = grl.announce_guru_run_log_destinations(ty, na, use_print=True)
    out = capsys.readouterr().out
    assert t_line in out
    assert n_line in out
    assert "tyrex_pm logging to" in t_line and "run_tyrex.log" in t_line
    assert "nautilus logging to" in n_line and "run_nautilus.log" in n_line
