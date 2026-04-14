"""
WP2 — framework-backed :class:`TradableStateHealthSnapshot` producer (Path B thin bridge).

**Signal path (pinned Nautilus):** ``LiveExecutionEngine._startup_reconciliation_event``
(``asyncio.Event``). Nautilus sets this event in the ``finally`` block of
``reconcile_execution_state`` (``nautilus_trader/live/execution_engine.py``) after the
initial mass-status / startup reconciliation pass completes. The same event is what
``_continuous_reconciliation_loop`` awaits before running periodic open/position checks.

**Mapping (frozen enum contract):**

- Event **not** set → ``UNKNOWN_BOOTSTRAP`` / ``nautilus_exec_startup_reconciliation_pending``
- Event **set** → ``HEALTHY`` / ``nautilus_exec_startup_reconciliation_complete``

**Framework-first:** Tyrex does not parse logs, re-run venue reconciliation, or infer OMS state
from cache diffs. It reads **one** documented engine field that Nautilus itself uses as a
bootstrap latch.

**Known gap (honest):** Mass-status reconciliation may **fail** while the event is still set
(``finally`` always runs). Nautilus does not expose a public boolean “reconciliation succeeded”
on the pinned stack, so ``HEALTHY`` here means **“startup reconciliation pass finished”**,
not **“zero discrepancies”**. ``DIVERGENT_PERSISTENT`` / ``DEGRADED_OMS`` are **not** derived
from this signal until the framework exposes machine-readable discrepancy / degraded state
(see ``tradable_state_health.md`` §15.2).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from tyrex_pm.runtime.tradable_state.provider import TradableStateHealthSource
from tyrex_pm.runtime.tradable_state.types import TradableStateHealth, TradableStateHealthSnapshot

_REASON_PENDING = "nautilus_exec_startup_reconciliation_pending"
_REASON_COMPLETE = "nautilus_exec_startup_reconciliation_complete"
_DETAIL_COMPLETE = (
    "LiveExecutionEngine startup reconciliation latch set "
    "(reconcile_execution_state finally; see nautilus_trader/live/execution_engine.py). "
    "Not a guarantee that mass-status reconciliation returned success — only that the "
    "engine completed its initial pass and unblocks continuous checks."
)


class NautilusLiveExecutionHealthSource:
    """
    Produces snapshots from ``exec_engine._startup_reconciliation_event`` (duck-typed engine).
    """

    __slots__ = ("_exec_engine",)

    def __init__(self, exec_engine: Any) -> None:
        ev = getattr(exec_engine, "_startup_reconciliation_event", None)
        if not isinstance(ev, asyncio.Event):
            raise TypeError(
                "NautilusLiveExecutionHealthSource requires exec_engine._startup_reconciliation_event "
                f"to be asyncio.Event (Nautilus LiveExecutionEngine); got {type(ev).__qualname__}.",
            )
        self._exec_engine = exec_engine

    def snapshot(self) -> TradableStateHealthSnapshot:
        ev: asyncio.Event = self._exec_engine._startup_reconciliation_event
        now = datetime.now(tz=UTC)
        if not ev.is_set():
            return TradableStateHealthSnapshot(
                level=TradableStateHealth.UNKNOWN_BOOTSTRAP,
                reason_code=_REASON_PENDING,
                observed_at_utc=now,
                framework_detail=(
                    "awaiting LiveExecutionEngine._startup_reconciliation_event "
                    "(initial reconcile_execution_state pass)"
                ),
            )
        return TradableStateHealthSnapshot(
            level=TradableStateHealth.HEALTHY,
            reason_code=_REASON_COMPLETE,
            observed_at_utc=now,
            framework_detail=_DETAIL_COMPLETE,
        )
