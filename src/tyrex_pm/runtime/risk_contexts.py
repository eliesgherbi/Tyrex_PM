from __future__ import annotations

from tyrex_pm.core.enums import ExecutionMode
from tyrex_pm.core.models import RiskContext


def live_unwired_fail_closed_context() -> RiskContext:
    """Venue/CLOB not connected — deny until Phase 11 wiring."""
    return RiskContext(
        execution_mode=ExecutionMode.LIVE,
        wallet_positions=(),
        open_orders=(),
        usdc_balance=None,
        usdc_allowance=None,
        last_wallet_sync_ts=None,
        mark_prices={},
        kill_switch=False,
        health_ok=False,
        heartbeat_ok=False,
        clob_session_ok=False,
        in_flight_order_count=0,
        orders_in_flight_by_token={},
        reconcile_drift=False,
    )
