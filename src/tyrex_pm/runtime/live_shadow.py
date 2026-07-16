"""Live public-data shadow runner (R5/R5.1 — ShadowOMS only; no private/trading APIs)."""

from __future__ import annotations

from tyrex_pm.runtime.config import ObserveConfig
from tyrex_pm.runtime.live_runner import run_live
from tyrex_pm.runtime.observe_host import ObserveRunResult


async def run_live_shadow(config: ObserveConfig) -> ObserveRunResult:
    return await run_live(config, enable_oms=True)
