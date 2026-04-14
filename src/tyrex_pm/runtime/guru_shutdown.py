"""``run_guru`` shutdown — Phase 4 drain before ``TradingNode.stop`` (``shutdown_drain.md``)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.config.loaders import RuntimeSettings
from tyrex_pm.runtime.guru_compose import GuruTradingAssembly
from tyrex_pm.runtime.lifecycle.shutdown_drain import ShutdownDrainCoordinator, ShutdownDrainResult


def drain_before_node_stop(
    assembly: GuruTradingAssembly,
    runtime: RuntimeSettings,
    run_context: Any | None,
) -> ShutdownDrainResult:
    """
    Cancel-and-drain (live) or record skip (shadow) — single call site for ``scripts/run_guru.py`` ``finally``.
    """
    rid = run_context.run_id if run_context is not None else None
    emit_fn = run_context.emit if run_context is not None else None
    return ShutdownDrainCoordinator(runtime).run(
        strategy=assembly.guru_strategy,
        lifecycle=assembly.execution_lifecycle,
        execution_reader=assembly.execution_state,
        fact_emit=emit_fn,
        run_id=rid,
        run_context=run_context,
    )
