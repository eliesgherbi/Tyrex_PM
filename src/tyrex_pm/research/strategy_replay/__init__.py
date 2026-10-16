"""Closed-loop strategy replay over recorded warehouse windows (research only).

Not an exchange simulator: execution follows the named assumptions of a
``strategy_closed_loop_v1`` scenario. Imports nothing from the live gateway.
"""

from tyrex_pm.research.strategy_replay.loader import (
    WindowCatalog,
    WindowRef,
    list_windows,
    load_window,
)
from tyrex_pm.research.strategy_replay.results import decision_digest, write_result
from tyrex_pm.research.strategy_replay.run import run_window

__all__ = [
    "WindowCatalog",
    "WindowRef",
    "decision_digest",
    "list_windows",
    "load_window",
    "run_window",
    "write_result",
]
