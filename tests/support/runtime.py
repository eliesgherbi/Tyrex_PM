"""A TradingRuntime with every declared field at its default, for tests of single methods.

Tests used to build ``object.__new__(TradingRuntime)`` and set a handful of attributes; the
host then had to probe with ``getattr`` for everything else. Fields are declared now, so a
bare runtime starts from the declared defaults (required fields are None).
"""

from __future__ import annotations

import dataclasses
from typing import Any


def bare_runtime(**attributes: Any) -> Any:
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    runtime = object.__new__(TradingRuntime)
    for item in dataclasses.fields(TradingRuntime):
        if item.default is not dataclasses.MISSING:
            value = item.default
        elif item.default_factory is not dataclasses.MISSING:
            value = item.default_factory()
        else:
            value = None
        object.__setattr__(runtime, item.name, value)
    for name, value in attributes.items():
        setattr(runtime, name, value)
    return runtime


def stand_in_runtime(**attributes: Any) -> Any:
    """A bare runtime for tests that only exercise one host path (tick, shutdown).

    Window summaries, late-evidence polling and the shutdown residual hold are stubbed:
    those tests used a namespace without these methods, so they never ran there.
    """
    from unittest.mock import AsyncMock, Mock

    stubs = {
        "_summarize_window": Mock(),
        "_check_earlier_attempts": AsyncMock(),
        "_shutdown_residual_hold": AsyncMock(return_value=False),
    }
    return bare_runtime(**{**stubs, **attributes})
