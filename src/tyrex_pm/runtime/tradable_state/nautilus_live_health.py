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
from typing import TYPE_CHECKING, Any

from tyrex_pm.runtime.tradable_state.provider import TradableStateHealthSource
from tyrex_pm.runtime.tradable_state.types import TradableStateHealth, TradableStateHealthSnapshot

if TYPE_CHECKING:
    from tyrex_pm.runtime.wallet_sync import WalletSyncHealthAdapter

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
    Produces snapshots from ``exec_engine._startup_reconciliation_event`` (duck-typed engine)
    and optional ``WalletSyncHealthAdapter`` for steady-state wallet sync health.

    Rule evaluation order (first match wins):

    1. Engine reconciliation not done → ``UNKNOWN_BOOTSTRAP``
    2. Wallet sync pending, deadline not exceeded → ``UNKNOWN_BOOTSTRAP``
    3. Wallet sync pending, deadline exceeded → ``DEGRADED_OMS``
    4. Wallet sync: terminally unresolvable instruments → ``DEGRADED_OMS``
    5. Wallet sync: stale or consecutive failures → ``DEGRADED_OMS``
    6. Both 4 and 5 apply → ``DEGRADED_OMS`` / ``wallet_sync_stale`` (more urgent)
    7. Otherwise → ``HEALTHY``
    """

    __slots__ = ("_exec_engine", "_wallet_sync_status")

    def __init__(
        self,
        exec_engine: Any,
        *,
        wallet_sync_status: WalletSyncHealthAdapter | None = None,
    ) -> None:
        ev = getattr(exec_engine, "_startup_reconciliation_event", None)
        if not isinstance(ev, asyncio.Event):
            raise TypeError(
                "NautilusLiveExecutionHealthSource requires exec_engine._startup_reconciliation_event "
                f"to be asyncio.Event (Nautilus LiveExecutionEngine); got {type(ev).__qualname__}.",
            )
        self._exec_engine = exec_engine
        self._wallet_sync_status = wallet_sync_status

    def snapshot(self) -> TradableStateHealthSnapshot:
        ev: asyncio.Event = self._exec_engine._startup_reconciliation_event
        now = datetime.now(tz=UTC)

        # Rule 1: Engine startup reconciliation not done
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

        ws = self._wallet_sync_status
        if ws is not None:
            # Rule 2: Wallet sync pending, deadline not exceeded
            if not ws.first_sync_complete and not ws.startup_deadline_exceeded:
                return TradableStateHealthSnapshot(
                    level=TradableStateHealth.UNKNOWN_BOOTSTRAP,
                    reason_code="wallet_sync_pending",
                    observed_at_utc=now,
                    framework_detail=(
                        "WalletSyncActor first cycle has not completed; "
                        "startup deadline not yet exceeded"
                    ),
                )

            # Rule 3: Wallet sync pending, deadline exceeded
            if not ws.first_sync_complete and ws.startup_deadline_exceeded:
                return TradableStateHealthSnapshot(
                    level=TradableStateHealth.DEGRADED_OMS,
                    reason_code="wallet_sync_startup_timeout",
                    observed_at_utc=now,
                    framework_detail=(
                        "WalletSyncActor first cycle not complete after startup deadline; "
                        "cache may be missing wallet instruments"
                    ),
                )

            # Rules 4+5: check both, prefer stale (rule 6)
            has_unresolvable = ws.terminally_unresolvable_count > 0
            stale = self._is_wallet_sync_stale(ws, now)

            if stale and has_unresolvable:
                return TradableStateHealthSnapshot(
                    level=TradableStateHealth.DEGRADED_OMS,
                    reason_code="wallet_sync_stale",
                    observed_at_utc=now,
                    framework_detail=(
                        f"wallet sync stale AND {ws.terminally_unresolvable_count} "
                        "terminally unresolvable instruments"
                    ),
                )
            if stale:
                return TradableStateHealthSnapshot(
                    level=TradableStateHealth.DEGRADED_OMS,
                    reason_code="wallet_sync_stale",
                    observed_at_utc=now,
                    framework_detail=(
                        "wallet sync cycle stale or consecutive failure threshold reached"
                    ),
                )
            if has_unresolvable:
                return TradableStateHealthSnapshot(
                    level=TradableStateHealth.DEGRADED_OMS,
                    reason_code="wallet_sync_unresolvable_instruments",
                    observed_at_utc=now,
                    framework_detail=(
                        f"{ws.terminally_unresolvable_count} condition_ids "
                        "terminally unresolvable after bounded retries"
                    ),
                )

        # Rule 7: healthy
        return TradableStateHealthSnapshot(
            level=TradableStateHealth.HEALTHY,
            reason_code=_REASON_COMPLETE,
            observed_at_utc=now,
            framework_detail=_DETAIL_COMPLETE,
        )

    @staticmethod
    def _is_wallet_sync_stale(
        ws: WalletSyncHealthAdapter,
        now: datetime,
    ) -> bool:
        if ws.consecutive_failure_count >= 3:
            return True
        last = ws.last_successful_cycle_utc
        if last is None:
            return False
        age = (now - last).total_seconds()
        return age > 2 * ws.poll_interval_seconds
