from __future__ import annotations

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.runtime.config import RuntimeConfig


def is_live(rt: RuntimeConfig) -> bool:
    return rt.execution_mode == ExecutionMode.LIVE
