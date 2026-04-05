"""delta_reason_code taxonomy (SCH-03)."""

from __future__ import annotations

from tyrex_pm.core.reason_codes import ReasonCode

# Stable strings for guru-vs-us / lost-notional grouping.
DELTA_REASON_UNKNOWN = "unknown"


_REASON_TO_DELTA: dict[str, str] = {
    ReasonCode.NOT_ALLOWLISTED: "token_filter",
    ReasonCode.MISSING_TOKEN_ID: "entry_exit_policy",
    ReasonCode.COPY_SKIP: "operator_config",
    ReasonCode.MIN_FOLLOW_NOTIONAL: "min_follow_notional",
    ReasonCode.MIN_FOLLOW_NOTIONAL_PRICE_MISSING: "min_follow_price_missing",
    ReasonCode.UNSUPPORTED_SIDE: "entry_exit_policy",
    ReasonCode.RISK_KILL_SWITCH: "operator_config",
    ReasonCode.RISK_ORDER_QTY_LIMIT: "operator_config",
    ReasonCode.RISK_NOTIONAL_PER_ORDER: "operator_config",
    ReasonCode.RISK_TOKEN_NOTIONAL_OPEN: "token_notional_cap",
    ReasonCode.RISK_MISSING_PRICE: "entry_exit_policy",
    ReasonCode.EXEC_ENTRY_GUARD_SKIP: "entry_guard_slippage",
    ReasonCode.EXEC_VENUE_NORMALIZE_SKIP: "normalization_min_size",
    ReasonCode.EXEC_BOOK_UNAVAILABLE_SKIP: "book_unavailable",
    ReasonCode.EXEC_LIMIT_TIMEOUT_CANCEL: "limit_timeout_cancel",
    ReasonCode.EXEC_DEPTH_CLIP_APPLIED: "depth_clip",
    ReasonCode.GURU_INSTRUMENT_UNMAPPED: "instrument_unmapped",
    ReasonCode.GURU_INSTRUMENT_NOT_IN_CACHE: "instrument_not_in_cache",
    ReasonCode.GURU_DYNAMIC_RESOLVE_FAILED: "dynamic_resolve_failed",
    ReasonCode.GURU_DYNAMIC_ACTIVATION_CAP: "activation_cap",
    ReasonCode.RISK_ACCOUNT_UNAVAILABLE: "stale_balance",
    ReasonCode.RISK_ALLOWANCE_UNAVAILABLE: "stale_balance",
    ReasonCode.RISK_INSUFFICIENT_COLLATERAL_BALANCE: "operator_config",
    ReasonCode.RISK_INSUFFICIENT_ALLOWANCE: "operator_config",
    ReasonCode.RISK_POSITION_EXPOSURE_UNRESOLVED: "pending_exposure",
    ReasonCode.RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED: "portfolio_cap",
    ReasonCode.RISK_PORTFOLIO_EXPOSURE_UNRESOLVED: "portfolio_exposure_unresolved",
    ReasonCode.RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT: "concurrent_cap",
    ReasonCode.RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE: "collateral_reserve",
    ReasonCode.LIVE_ORDER_ERROR: "venue_error",
}


_GATE_TO_DELTA: dict[str, str] = {
    "portfolio": "portfolio_exposure_unresolved",
    "portfolio_unresolved": "portfolio_exposure_unresolved",
    "portfolio_cap": "portfolio_cap",
    "guru_concurrent": "concurrent_cap",
    "reserve": "collateral_reserve",
    "min_collateral": "operator_config",
    "min_allowance": "operator_config",
}


def to_delta_reason(reason_code: str, gate: str | None = None) -> str:
    """Map Tyrex reason / risk gate to delta_reason_code."""
    if gate:
        g = str(gate).strip().lower()
        for prefix, delta in _GATE_TO_DELTA.items():
            if g.startswith(prefix):
                return delta
    rc = str(reason_code)
    return _REASON_TO_DELTA.get(rc, DELTA_REASON_UNKNOWN)


# Success-path / telemetry codes not used for delta_reason (skipped in strict lint).
_DELTA_EXCLUDED: frozenset[str] = frozenset(
    {
        ReasonCode.GURU_ENTRY_CANDIDATE,
        ReasonCode.GURU_EXIT_MIRROR,
        ReasonCode.SHADOW_ORDER_INTENT,
        ReasonCode.LIVE_ORDER_SUBMIT,
    },
)


def unmapped_reason_codes() -> list[str]:
    """For CI (VAL-06): ReasonCode members missing from _REASON_TO_DELTA excluding success codes."""
    missing: list[str] = []
    for m in ReasonCode:
        v = m.value
        if v in _DELTA_EXCLUDED:
            continue
        if v not in _REASON_TO_DELTA:
            missing.append(v)
    return missing
