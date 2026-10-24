"""The trading host stays split by responsibility (v1 step 2): no component regrows."""

from __future__ import annotations

from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[3] / "src" / "tyrex_pm" / "runtime"


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_trading_runtime_is_a_thin_host() -> None:
    assert _lines(RUNTIME / "trading_runtime.py") <= 800


def test_each_host_component_stays_focused() -> None:
    sizes = {path.name: _lines(path) for path in (RUNTIME / "host").glob("*.py")}
    assert sizes and max(sizes.values()) <= 700, sizes


def test_the_host_does_not_probe_itself() -> None:
    for path in [RUNTIME / "trading_runtime.py", *(RUNTIME / "host").glob("*.py")]:
        assert "getattr(self," not in path.read_text(encoding="utf-8"), path.name
