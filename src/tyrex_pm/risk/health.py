from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tyrex_pm.core import reason_codes as rc
from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.models import RiskContext
from tyrex_pm.runtime.config import ReadinessConfig, RuntimeConfig


def check_readiness(
    ctx: RiskContext,
    *,
    runtime: RuntimeConfig,
    readiness: ReadinessConfig,
) -> tuple[bool, str | None]:
    if not ctx.health_ok:
        return False, rc.RECONCILE_DRIFT if ctx.reconcile_drift else rc.NOT_READY
    if readiness.require_wallet_sync:
        if ctx.last_wallet_sync_ts is None:
            return False, rc.NOT_READY
        age = datetime.now(timezone.utc) - ctx.last_wallet_sync_ts
        if age > timedelta(seconds=readiness.max_wallet_age_s_live):
            return False, rc.STALE_WALLET_SNAPSHOT
    if runtime.execution_mode == ExecutionMode.LIVE:
        if readiness.require_heartbeat_live and not ctx.heartbeat_ok:
            return False, rc.HEARTBEAT_FAILED
        if not ctx.clob_session_ok:
            return False, rc.NOT_READY
    return True, None


def check_aggressive_readiness(
    ctx: RiskContext,
    *,
    runtime: RuntimeConfig,
    readiness: ReadinessConfig,
) -> tuple[bool, str | None]:
    """Live new-order path: readiness + user-stream freshness (when required)."""
    ok, reason = check_readiness(ctx, runtime=runtime, readiness=readiness)
    if not ok:
        return False, reason
    if runtime.execution_mode == ExecutionMode.LIVE and readiness.require_user_ws_live:
        if ctx.venue_truth_stale:
            return False, rc.VENUE_TRUTH_STALE
    return True, None
