"""R5.1: one evaluate pipeline; mode only changes OMS dispatch."""

from __future__ import annotations

import ast
from pathlib import Path

import tyrex_pm
from tyrex_pm.runtime.observe_host import ObserveHost, TradingHost
from tyrex_pm.runtime.shadow_host import ShadowHost


def test_trading_host_alias() -> None:
    assert TradingHost is ObserveHost


def test_shadow_host_is_observe_subclass() -> None:
    assert issubclass(ShadowHost, ObserveHost)


def test_shadow_does_not_redefine_evaluate_once_body_as_duplicate_pipeline() -> None:
    """ShadowHost must not contain a second snapshot→signal pipeline."""
    text = (Path(tyrex_pm.__file__).parent / "runtime" / "shadow_host.py").read_text(
        encoding="utf-8"
    )
    # Must not call build_directional_signal (that lives only in ObserveHost.evaluate_once)
    assert "build_directional_signal" not in text
    assert "def evaluate_once" not in text


def test_observe_host_has_hooks() -> None:
    src = (Path(tyrex_pm.__file__).parent / "runtime" / "observe_host.py").read_text(
        encoding="utf-8"
    )
    assert "def _build_decision_context" in src
    assert "def _process_transition" in src
    assert "TradingHost = ObserveHost" in src


def test_live_runners_share_live_runner() -> None:
    observe = (
        Path(tyrex_pm.__file__).parent / "runtime" / "live_observe.py"
    ).read_text(encoding="utf-8")
    shadow = (
        Path(tyrex_pm.__file__).parent / "runtime" / "live_shadow.py"
    ).read_text(encoding="utf-8")
    assert "from tyrex_pm.runtime.live_runner import run_live" in observe
    assert "from tyrex_pm.runtime.live_runner import run_live" in shadow
    assert "PolymarketMarketWsAdapter" not in observe
    assert "PolymarketMarketWsAdapter" not in shadow
