"""Stable risk / health reason codes for facts and logs."""

from __future__ import annotations

# Pre-trade / notional
NOTIONAL_BELOW_MIN = "notional_below_min"
NOTIONAL_ABOVE_MAX = "notional_above_max"
#: Final post-clip order ``size`` is below the venue's minimum order size (e.g. Polymarket's
#: hard 5-share floor). Set by :mod:`tyrex_pm.risk.venue_min_size`. Two sub-cases share the code:
#: ``policy=deny`` short-circuits here; ``policy=bump`` falls through and only emits this
#: code when the bumped size would itself break a higher-priority cap/capital constraint.
BELOW_VENUE_MIN_SIZE = "below_venue_min_size"

# Deployment
TOKEN_DEPLOYMENT_CAP = "token_deployment_cap"
PORTFOLIO_DEPLOYMENT_CAP = "portfolio_deployment_cap"
DEPLOYMENT_MARK_UNKNOWN = "deployment_mark_unknown"

# Capital / inventory
INSUFFICIENT_CAPITAL = "insufficient_capital"
INSUFFICIENT_ALLOWANCE = "insufficient_allowance"
NAKED_SELL = "naked_sell"
INSUFFICIENT_INVENTORY = "insufficient_inventory"

# Kill / concurrency / health
KILL_SWITCH = "kill_switch"
CONCURRENCY_LIMIT = "concurrency_limit"
NOT_READY = "not_ready"
STALE_WALLET_SNAPSHOT = "stale_wallet_snapshot"
RECONCILE_DRIFT = "reconcile_drift"
HEARTBEAT_FAILED = "heartbeat_failed"
VENUE_TRUTH_STALE = "venue_truth_stale"

# Guru / strategy filters
TOKEN_NOT_ALLOWLISTED = "token_not_allowlisted"
GURU_BELOW_MIN_NOTIONAL = "guru_below_min_notional"
GURU_SIGNIFICANCE_REJECT = "guru_significance_reject"
GURU_LOW_CONVICTION = "guru_low_conviction"
GURU_EXIT_BELOW_DUST = "guru_exit_below_dust"
GURU_NO_BOT_INVENTORY = "guru_no_bot_inventory"
GURU_PRICE_REQUIRED = "guru_price_required"
GURU_STATIC_AMOUNT_INVALID = "guru_static_amount_invalid"
MARKET_UNTRADEABLE = "market_untradeable"
MARKET_METADATA_UNAVAILABLE = "market_metadata_unavailable"

# Submit / repair guards
DUPLICATE_SUBMIT_BLOCKED = "duplicate_submit_blocked"
VENUE_RESTART_SUSPECTED = "venue_restart_suspected"

# Generic
APPROVED = "approved"
UNKNOWN = "unknown"
