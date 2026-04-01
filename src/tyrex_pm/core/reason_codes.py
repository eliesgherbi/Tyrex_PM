"""Stable `reason_code` strings for copy telemetry (shadow + live)."""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    """Use `.value` in logs; enum gives refactor safety in Python."""

    GURU_ENTRY_CANDIDATE = "guru_entry_candidate"
    GURU_EXIT_MIRROR = "guru_exit_mirror"
    NOT_ALLOWLISTED = "not_allowlisted"
    MISSING_TOKEN_ID = "missing_token_id"
    COPY_SKIP = "copy_skip"
    SHADOW_ORDER_INTENT = "shadow_order_intent"
    UNSUPPORTED_SIDE = "unsupported_side"
    RISK_KILL_SWITCH = "risk_kill_switch"
    RISK_ORDER_QTY_LIMIT = "risk_order_qty_limit"
    RISK_NOTIONAL_PER_ORDER = "risk_notional_per_order"
    RISK_TOKEN_NOTIONAL_OPEN = "risk_token_notional_open"
    RISK_MISSING_PRICE = "risk_missing_price"
    LIVE_ORDER_SUBMIT = "live_order_submit"
    LIVE_ORDER_ERROR = "live_order_error"
