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
#: Urgent reduce-only SELL approved despite missing deployment mark; executable bid used.
REDUCE_ONLY_EXIT_MARK_FALLBACK = "reduce_only_exit_mark_fallback"

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
#: Live mode only: blocks the very first risk evaluation until the V2 venue truth
#: has been rebuilt at least once (first successful ``refresh_wallet_from_clob`` +
#: heartbeat ok + user-WS first message or stale-grace elapsed). Prevents a stale
#: in-memory or on-disk state snapshot from seeding the first V2 process. Cleared
#: by ``HealthRuntime.mark_first_v2_sync_complete()`` from ``venue_refresh_loop``.
BOOTSTRAP_NOT_COMPLETE = "bootstrap_not_complete"

# Guru / strategy filters
TOKEN_NOT_ALLOWLISTED = "token_not_allowlisted"
GURU_BELOW_MIN_NOTIONAL = "guru_below_min_notional"
GURU_SIGNIFICANCE_REJECT = "guru_significance_reject"
GURU_LOW_CONVICTION = "guru_low_conviction"
GURU_EXIT_BELOW_DUST = "guru_exit_below_dust"
GURU_NO_BOT_INVENTORY = "guru_no_bot_inventory"
GURU_NO_ALLOCATED_INVENTORY = "guru_no_allocated_inventory"
GURU_PRICE_REQUIRED = "guru_price_required"
GURU_STATIC_AMOUNT_INVALID = "guru_static_amount_invalid"
MARKET_UNTRADEABLE = "market_untradeable"
MARKET_METADATA_UNAVAILABLE = "market_metadata_unavailable"

# Submit / repair guards
DUPLICATE_SUBMIT_BLOCKED = "duplicate_submit_blocked"
VENUE_RESTART_SUSPECTED = "venue_restart_suspected"

# Execution planner (P3 architecture_enhance)
PLANNER_PASSIVE_ENTRY = "planner_passive_entry"
PLANNER_NORMAL_ENTRY = "planner_normal_entry"
PLANNER_PAIRED_ENTRY_FAK = "planner_paired_entry_fak"
PLANNER_PAIRED_ENTRY_FOK = "planner_paired_entry_fok"
PAIRED_ENTRY_STYLE_UNSUPPORTED = "paired_entry_style_unsupported"
PAIR_PREFLIGHT_REJECTED = "pair_preflight_rejected"
PLANNER_PASSIVE_EXIT = "planner_passive_exit"
PLANNER_URGENT_EXIT_FAK = "planner_urgent_exit_fak"
PLANNER_URGENT_EXIT_FALLBACK = "planner_urgent_exit_fallback"
#: Urgent/protection exit denied because the book is stale (older than max_book_age_s).
PLANNER_STALE_BOOK = "planner_stale_book"
#: Urgent/protection exit denied because no book exists for the token.
PLANNER_MISSING_BOOK = "planner_missing_book"
#: Planner enabled but no market data provider supplied for an exit that needs one.
PLANNER_NO_MARKET_DATA = "planner_no_market_data"
#: Final validation: the planned price is adverse vs the pre-check approved price.
PLANNER_PRICE_WORSENED = "planner_price_worsened"
#: Final validation: planned notional exceeds the max even though cap policy was set.
PLANNER_NOTIONAL_VIOLATION = "planner_notional_violation"
#: Final validation: venue-min-size would require resizing a finalized plan.
PLANNER_RESIZE_NOT_ALLOWED = "planner_resize_not_allowed"
PLANNER_UNSUPPORTED_INTENT = "planner_unsupported_intent"

# Paired binary entry (Phase 4.6)
PAIR_COST_TOO_HIGH = "pair_cost_too_high"
YES_SPREAD_TOO_WIDE = "yes_spread_too_wide"
NO_SPREAD_TOO_WIDE = "no_spread_too_wide"
YES_BOOK_STALE = "yes_book_stale"
NO_BOOK_STALE = "no_book_stale"
MISSING_YES_ASK = "missing_yes_ask"
MISSING_NO_ASK = "missing_no_ask"
MISSING_YES_BID = "missing_yes_bid"
MISSING_NO_BID = "missing_no_bid"
YES_SPREAD_EXCEEDS_LOSS_BUDGET = "yes_spread_exceeds_loss_budget"
NO_SPREAD_EXCEEDS_LOSS_BUDGET = "no_spread_exceeds_loss_budget"
YES_ACTIVATION_GAP_EXCEEDS_LOSS_BUDGET = "yes_activation_gap_exceeds_loss_budget"
NO_ACTIVATION_GAP_EXCEEDS_LOSS_BUDGET = "no_activation_gap_exceeds_loss_budget"
PAIRED_BINARY_NOT_IDLE = "paired_binary_not_idle"

# Generic
APPROVED = "approved"
UNKNOWN = "unknown"
