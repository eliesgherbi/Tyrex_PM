"""Protection supervisor loop (P4.5 live wiring).

Periodically calls ``ProtectionMonitor.tick`` and routes exit work through the
generic pipeline. Procedural now; event-ready later via market/fill subscriptions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tyrex_pm.core.ids import RunId
from tyrex_pm.core.time import monotonic_s
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.schema_v2 import FACT_TYPE_HEALTH
from tyrex_pm.runtime.config import AppConfig, ProtectionRuntimeConfig
from tyrex_pm.runtime.protection_runtime import run_protection_tick


@dataclass(frozen=True)
class ProtectionSupervisorResult:
    ticks: int
    triggered: bool
    exit_submitted: bool
    stopped_reason: str
    runtime_s: float


async def run_protection_supervisor(
    coord,
    app: AppConfig,
    *,
    run_id: RunId,
    strategy: object,
    sink,
    oms,
    stop: asyncio.Event | None = None,
    apply_local_shadow_fill: bool = False,
    live_clob_client: object | None = None,
    prot: ProtectionRuntimeConfig | None = None,
) -> ProtectionSupervisorResult:
    cfg = prot or app.protection
    if cfg is None or not cfg.enabled:
        return ProtectionSupervisorResult(0, False, False, "protection_disabled", 0.0)

    interval = max(0.1, float(cfg.tick_interval_s))
    max_runtime = max(interval, float(cfg.max_runtime_s))
    start = monotonic_s()
    ticks = 0
    triggered = False
    exit_submitted = False
    stopped_reason = "max_runtime"

    while monotonic_s() - start < max_runtime:
        if stop is not None and stop.is_set():
            stopped_reason = "stop_event"
            break

        work = await run_protection_tick(
            coord,
            app,
            run_id=run_id,
            strategy=strategy,
            sink=sink,
            oms=oms,
            apply_local_shadow_fill=apply_local_shadow_fill,
            live_clob_client=live_clob_client,
        )
        ticks += 1

        if work:
            triggered = True
            if cfg.stop_after_trigger and cfg.stop_after_exit_submit:
                exit_submitted = True
                stopped_reason = "exit_submit"
                break
            if cfg.stop_after_trigger and not cfg.stop_after_exit_submit:
                stopped_reason = "trigger"
                break

        try:
            if stop is not None:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                stopped_reason = "stop_event"
                break
        except asyncio.TimeoutError:
            pass

    runtime_s = monotonic_s() - start
    if not triggered and not exit_submitted and monotonic_s() - start >= max_runtime:
        if cfg.fail_if_no_trigger:
            stopped_reason = "timeout_no_trigger"

    sink.write(
        make_fact(
            FACT_TYPE_HEALTH,
            str(run_id),
            {
                "event": "protection_supervisor_stopped",
                "ticks": ticks,
                "triggered": triggered,
                "exit_submitted": exit_submitted,
                "reason": stopped_reason,
                "runtime_s": round(runtime_s, 3),
            },
        )
    )
    return ProtectionSupervisorResult(
        ticks=ticks,
        triggered=triggered,
        exit_submitted=exit_submitted,
        stopped_reason=stopped_reason,
        runtime_s=runtime_s,
    )
