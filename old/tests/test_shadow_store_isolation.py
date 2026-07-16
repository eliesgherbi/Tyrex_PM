"""Prove paired-binary paths never reference market_state_shadow."""

from __future__ import annotations

import inspect


def test_paired_binary_runtime_uses_market_state_not_shadow() -> None:
    from tyrex_pm.runtime import paired_binary_run as pbr

    src = inspect.getsource(pbr)
    assert "market_state_shadow" not in src
    assert "coord.market_state" in src


def test_entry_eval_uses_market_state_parameter_only() -> None:
    from tyrex_pm.strategies.paired_binary import entry_eval

    src = inspect.getsource(entry_eval)
    assert "market_state_shadow" not in src


def test_monitor_uses_coord_market_state_only() -> None:
    from tyrex_pm.strategies.paired_binary import monitor

    src = inspect.getsource(monitor)
    assert "market_state_shadow" not in src
    assert "coord.market_state" in src


def test_strategy_module_no_shadow_reference() -> None:
    from tyrex_pm.strategies.paired_binary import strategy

    src = inspect.getsource(strategy)
    assert "market_state_shadow" not in src


def test_feature_builder_not_imported_by_strategy_modules() -> None:
    from tyrex_pm.strategies.paired_binary import entry_eval, monitor, strategy

    for mod in (entry_eval, monitor, strategy):
        src = inspect.getsource(mod)
        assert "FeatureBuilder" not in src
        assert "feature_builder" not in src.lower()
