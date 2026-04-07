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
    #: C2 — estimated ``price_ref * qty`` below ``min_follow_notional_usd`` (policy).
    MIN_FOLLOW_NOTIONAL = "min_follow_notional"
    #: C2 — ``min_follow_notional_usd`` enabled but guru ``price_ref`` missing.
    MIN_FOLLOW_NOTIONAL_PRICE_MISSING = "min_follow_notional_price_missing"
    SHADOW_ORDER_INTENT = "shadow_order_intent"
    UNSUPPORTED_SIDE = "unsupported_side"
    RISK_KILL_SWITCH = "risk_kill_switch"
    #: **Legacy** (pre deployment-budget): emitted only when reading old facts / summaries.
    RISK_ORDER_QTY_LIMIT = "risk_order_qty_limit"
    #: **Legacy** — use :attr:`RISK_ORDER_DEPLOYMENT_EXCEEDED`.
    RISK_NOTIONAL_PER_ORDER = "risk_notional_per_order"
    #: **Deployment budget** — per-order cap: ``order_deploy > max_notional_usd_per_order``.
    RISK_ORDER_DEPLOYMENT_EXCEEDED = "risk_order_deployment_exceeded"
    #: **BUY** only — ``order_deploy`` below ``min_notional_usd_per_order`` (``min_notional_policy: deny``).
    RISK_MIN_ORDER_NOTIONAL = "risk_min_order_notional"
    #: ``min_notional_policy: cap`` would exceed ``max_notional_usd_per_order`` after bump (infeasible band).
    RISK_ORDER_DEPLOYMENT_INFEASIBLE = "risk_order_deployment_infeasible"
    #: **Legacy** — use :attr:`RISK_TOKEN_DEPLOYMENT_EXCEEDED`.
    RISK_TOKEN_NOTIONAL_OPEN = "risk_token_notional_open"
    #: **Deployment budget** — per-token cap breach.
    RISK_TOKEN_DEPLOYMENT_EXCEEDED = "risk_token_deployment_exceeded"
    RISK_MISSING_PRICE = "risk_missing_price"
    LIVE_ORDER_SUBMIT = "live_order_submit"
    LIVE_ORDER_ERROR = "live_order_error"
    #: C3 — execution: market moved worse than ``execution_max_entry_slippage_ticks``.
    EXEC_ENTRY_GUARD_SKIP = "exec_entry_guard_skip"
    #: **Legacy** telemetry — pre–P2 operator “venue normalize” skip; prefer :attr:`EXEC_INSTRUMENT_QUANTIZE_SKIP`.
    EXEC_VENUE_NORMALIZE_SKIP = "exec_venue_normalize_skip"
    #: Execution — cannot place limit on instrument tick/size/min-quantity grid without enlarging qty past risk intent.
    EXEC_INSTRUMENT_QUANTIZE_SKIP = "exec_instrument_quantize_skip"
    #: C3 — book required (strict) but unavailable for guard/clip.
    EXEC_BOOK_UNAVAILABLE_SKIP = "exec_book_unavailable_skip"
    #: C3 — canceled working limit after ``execution_limit_timeout_seconds`` (unfilled).
    EXEC_LIMIT_TIMEOUT_CANCEL = "exec_limit_timeout_cancel"
    #: C3 — logged when depth clip reduced size (diagnostic; submit still proceeds).
    EXEC_DEPTH_CLIP_APPLIED = "exec_depth_clip_applied"
    #: **Step 5:** Guru token_id has no bootstrap map and dynamic path off or unavailable.
    GURU_INSTRUMENT_UNMAPPED = "guru_instrument_unmapped"
    #: **Step 5:** Bootstrap map present but instrument missing from Cache (no dynamic recovery).
    GURU_INSTRUMENT_NOT_IN_CACHE = "guru_instrument_not_in_cache"
    #: **Step 5:** Gamma/CLOB/parse failed for dynamic resolution.
    GURU_DYNAMIC_RESOLVE_FAILED = "guru_dynamic_resolve_failed"
    #: **Step 5:** Session activation cap reached for dynamically added instruments.
    GURU_DYNAMIC_ACTIVATION_CAP = "guru_dynamic_activation_cap"
    #: Phase A — capital gate: ``Portfolio.account`` missing or unusable.
    RISK_ACCOUNT_UNAVAILABLE = "risk_account_unavailable"
    #: Phase A — py-clob allowance read missing when required.
    RISK_ALLOWANCE_UNAVAILABLE = "risk_allowance_unavailable"
    RISK_INSUFFICIENT_COLLATERAL_BALANCE = "risk_insufficient_collateral_balance"
    RISK_INSUFFICIENT_ALLOWANCE = "risk_insufficient_allowance"
    #: **Legacy** — token cap could not resolve ``net_exposure``; prefer :attr:`RISK_TOKEN_DEPLOYMENT_UNRESOLVED`.
    RISK_POSITION_EXPOSURE_UNRESOLVED = "risk_position_exposure_unresolved"
    #: **Deployment budget** — cannot compute token pending/filled deployment (strict).
    RISK_TOKEN_DEPLOYMENT_UNRESOLVED = "risk_token_deployment_unresolved"
    #: **Legacy** — portfolio cap over limit; use :attr:`RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED`.
    RISK_PORTFOLIO_NOTIONAL_CAP_EXCEEDED = "risk_portfolio_notional_cap_exceeded"
    #: **Deployment budget** — portfolio cap breach.
    RISK_PORTFOLIO_DEPLOYMENT_EXCEEDED = "risk_portfolio_deployment_exceeded"
    #: **Legacy** — marked-exposure B1 incomplete; removed from active risk path.
    RISK_PORTFOLIO_EXPOSURE_UNRESOLVED = "risk_portfolio_exposure_unresolved"
    #: **Deployment budget** — portfolio deployment accounting incomplete (pending/filled parse).
    RISK_PORTFOLIO_DEPLOYMENT_UNRESOLVED = "risk_portfolio_deployment_unresolved"
    #: Phase B B3 — concurrent open guru resting orders at/over ``max_concurrent_guru_resting_orders``.
    RISK_GURU_CONCURRENT_RESTING_ORDERS_LIMIT = "risk_guru_concurrent_resting_orders_limit"
    #: Phase B B4 — py-clob collateral ``balance`` below ``collateral_reserve_usd + n`` on BUY.
    RISK_INSUFFICIENT_FREE_COLLATERAL_AFTER_RESERVE = "risk_insufficient_free_collateral_after_reserve"
