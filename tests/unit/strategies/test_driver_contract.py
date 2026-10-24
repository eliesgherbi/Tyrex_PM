"""Every registered strategy satisfies the one driver contract every host relies on."""

from __future__ import annotations

import pytest
from support.strategy_configs import PARAMETERS, strategy_config

from tyrex_pm.core.clock import SystemClock
from tyrex_pm.strategies.base import BaseDriver
from tyrex_pm.strategies.protocol import StrategyDriver
from tyrex_pm.strategies.registry import get_strategy_plugin, registered_strategy_kinds
from tyrex_pm.strategies.triggers import EvaluationTrigger, evaluation_trigger


def test_every_registered_kind_has_test_parameters() -> None:
    assert set(registered_strategy_kinds()) == set(PARAMETERS)


@pytest.mark.parametrize("kind", sorted(PARAMETERS))
def test_driver_satisfies_the_contract(kind: str) -> None:
    plugin = get_strategy_plugin(kind)
    driver = plugin.create_driver(config=strategy_config(kind), clock=SystemClock())
    assert isinstance(driver, BaseDriver)
    assert isinstance(driver, StrategyDriver)
    # The optional half has working defaults: hosts call them without probing.
    driver.on_hold_committed()
    assert callable(driver.restore_window_usage)
    view = driver.episode_view()
    assert view is None or view.window_id == driver.window_id
    assert isinstance(driver.persistence_slice(), dict)


def test_triggers_are_one_vocabulary() -> None:
    assert [t.value for t in EvaluationTrigger] == ["feed", "timer", "replay", "observe"]
    assert evaluation_trigger("replay") is EvaluationTrigger.REPLAY
    assert str(EvaluationTrigger.FEED) == "feed"  # records keep plain strings
    with pytest.raises(ValueError, match="unknown evaluation trigger"):
        evaluation_trigger("tick")

