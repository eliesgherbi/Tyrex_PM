from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from tyrex_pm.core.time import utc_now
from tyrex_pm.state.reconcile import ReconcileResult


@dataclass
class HealthRuntime:
    """Reconciliation + live CLOB session/heartbeat (Phase 11)."""

    reconcile_drift: bool = False
    heartbeat_ok: bool = False
    #: Server-driven CLOB heartbeat session: None => next POST uses ""; str => next POST uses this id.
    clob_heartbeat_id_next: str | None = None
    #: Ensures one heartbeat logical tick at a time (no overlapping POSTs sharing session state).
    _heartbeat_send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    clob_session_ok: bool = False
    venue_truth_stale: bool = False
    venue_truth_inconsistent: bool = False
    user_ws_rest_only: bool = False
    user_ws_last_msg_ts: datetime | None = None
    #: Bumped when any submit/cancel returns a 425/matching-engine restart hint; suppresses
    #: provisional auto-resolution (UNKNOWN_TERMINAL) until cleared.
    venue_restart_suspected: bool = False
    venue_restart_last_ts: datetime | None = None
    #: Set to True after the first successful V2 venue truth rebuild
    #: (``refresh_wallet_from_clob`` ok). Until then, ``check_aggressive_readiness``
    #: denies live order evaluation with reason ``bootstrap_not_complete``.
    #: Defaults to False so a freshly constructed live ``HealthRuntime`` cannot
    #: accidentally let pre-bootstrap intents through.
    first_v2_sync_complete: bool = False

    def apply_reconcile(self, res: ReconcileResult) -> None:
        #: Fail-closed for **blocking** venue drift only (provisional grace is non-blocking).
        self.reconcile_drift = len(res.blocking_drift_flags) > 0
        self.venue_truth_inconsistent = len(res.drift_flags) > 0

    def mark_heartbeat(self, *, ok: bool) -> None:
        self.heartbeat_ok = ok
        if ok:
            self.clob_session_ok = True
        else:
            self.clob_session_ok = False

    def mark_user_ws_message(self, *, ts: datetime | None = None) -> None:
        self.user_ws_last_msg_ts = ts or utc_now()
        self.venue_truth_stale = False

    def mark_venue_restart_suspected(self, *, ts: datetime | None = None) -> None:
        """Operator/health hint: matching engine restart / 425 path observed; pause auto-resolve."""
        self.venue_restart_suspected = True
        self.venue_restart_last_ts = ts or utc_now()

    def clear_venue_restart_suspected(self) -> None:
        self.venue_restart_suspected = False

    def mark_first_v2_sync_complete(self) -> None:
        """Set after the first successful V2 venue truth rebuild.

        Idempotent. Called from ``live_supervisor.venue_refresh_loop`` once
        ``refresh_wallet_from_clob`` returns without raising.
        """
        self.first_v2_sync_complete = True
