"""Live public read-only observe / dry-plan runner (R3B/R4 — no OMS)."""

from __future__ import annotations

from tyrex_pm.runtime.config import ObserveConfig
from tyrex_pm.runtime.live_runner import run_live
from tyrex_pm.runtime.observe_host import ObserveRunResult


async def run_live_observe(config: ObserveConfig) -> ObserveRunResult:
    return await run_live(config, enable_oms=False)
