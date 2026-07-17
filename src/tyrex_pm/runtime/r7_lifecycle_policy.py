"""Authoritative R7B/R7E/R7F lifecycle policy constants.

Every value used by ``r7b-live-once`` exit/entry gating is defined here.
Do not bury these behind opaque “configured” language.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.execution.polymarket.lifecycle_exit_plan import (
    ExitPricePolicy,
    ExitRetryPolicy,
)
from tyrex_pm.execution.polymarket.settlement import (
    DEFAULT_MIN_TRADABLE,
    SettlementWaitConfig,
)


# --- Exit book / price -------------------------------------------------
BOOK_FRESHNESS_MAX_AGE_MS: int = 2000
NORMAL_EXIT_PRICE_FLOOR: Decimal = Decimal("0.01")
EMERGENCY_EXIT_PRICE_FLOOR: Decimal = Decimal("0.01")
# Worst accepted bid may not be more than this below best bid (normal exit).
MAX_EXIT_SLIPPAGE_FROM_TOUCH: Decimal = Decimal("0.05")
# Bid-ask spread ceiling at exit time.
MAX_EXIT_BOOK_SPREAD: Decimal | None = Decimal("0.20")
REQUIRE_FULL_BID_DEPTH: bool = True

# --- Exit retries ------------------------------------------------------
EXIT_MAX_ATTEMPTS: int = 3
EXIT_RETRY_COOLDOWN_S: float = 0.5
EXIT_RETRY_BACKOFF_MULTIPLIER: float = 1.5
EXIT_RETRY_MAX_COOLDOWN_S: float = 4.0

# --- Settlement wait (entry CONFIRMED) ---------------------------------
SETTLEMENT_MAX_WAIT_S: float = 45.0
SETTLEMENT_INITIAL_BACKOFF_S: float = 0.25
SETTLEMENT_MAX_BACKOFF_S: float = 4.0

# --- Window / deadlines (btc_5m) ---------------------------------------
FLATTEN_BEFORE_CLOSE_S: float = 30.0
ENTRY_SAFETY_BUFFER_S: float = 45.0
APPROVAL_SKEW_S: float = 5.0
MIN_REMAINING_FOR_ENTRY_S: float = 90.0
# Max hold defaults to time until flatten deadline (computed per window).
MAX_HOLD_S_FLOOR: float = 30.0

# --- Quantity / money --------------------------------------------------
QTY_STEP: Decimal = Decimal("0.01")
MONEY_STEP: Decimal = Decimal("0.01")
MIN_TRADABLE_QTY: Decimal = DEFAULT_MIN_TRADABLE  # 0.01
MAX_BUY_COLLATERAL: Decimal = Decimal("5.00")

# --- Strategy / CLI envelope -------------------------------------------
STRATEGY_ID: str = "reference-momentum"
MARKET_FAMILY: str = "btc_updown_5m"
MAX_WINDOWS: int = 3


def default_exit_price_policy() -> ExitPricePolicy:
    return ExitPricePolicy(
        normal_floor=NORMAL_EXIT_PRICE_FLOOR,
        emergency_floor=EMERGENCY_EXIT_PRICE_FLOOR,
        max_book_age_ms=BOOK_FRESHNESS_MAX_AGE_MS,
        require_full_depth=REQUIRE_FULL_BID_DEPTH,
        max_slippage_from_touch=MAX_EXIT_SLIPPAGE_FROM_TOUCH,
        max_book_spread=MAX_EXIT_BOOK_SPREAD,
    )


def default_exit_retry_policy() -> ExitRetryPolicy:
    return ExitRetryPolicy(
        max_attempts=EXIT_MAX_ATTEMPTS,
        cooldown_s=EXIT_RETRY_COOLDOWN_S,
        backoff_multiplier=EXIT_RETRY_BACKOFF_MULTIPLIER,
        max_cooldown_s=EXIT_RETRY_MAX_COOLDOWN_S,
    )


def default_settlement_wait_config() -> SettlementWaitConfig:
    return SettlementWaitConfig(
        max_wait_s=SETTLEMENT_MAX_WAIT_S,
        initial_backoff_s=SETTLEMENT_INITIAL_BACKOFF_S,
        max_backoff_s=SETTLEMENT_MAX_BACKOFF_S,
        max_sell_attempts=1,  # per-submit; outer loop uses EXIT_MAX_ATTEMPTS
    )


def policy_snapshot() -> dict[str, Any]:
    """Exact values for reports / operator handoff."""
    return {
        "book_freshness_max_age_ms": BOOK_FRESHNESS_MAX_AGE_MS,
        "normal_exit_price_floor": str(NORMAL_EXIT_PRICE_FLOOR),
        "emergency_exit_price_floor": str(EMERGENCY_EXIT_PRICE_FLOOR),
        "max_exit_slippage_from_touch": str(MAX_EXIT_SLIPPAGE_FROM_TOUCH),
        "max_exit_book_spread": (
            None if MAX_EXIT_BOOK_SPREAD is None else str(MAX_EXIT_BOOK_SPREAD)
        ),
        "require_full_bid_depth": REQUIRE_FULL_BID_DEPTH,
        "settlement_max_wait_s": SETTLEMENT_MAX_WAIT_S,
        "settlement_initial_backoff_s": SETTLEMENT_INITIAL_BACKOFF_S,
        "settlement_max_backoff_s": SETTLEMENT_MAX_BACKOFF_S,
        "exit_retry_cooldown_s": EXIT_RETRY_COOLDOWN_S,
        "exit_retry_backoff_multiplier": EXIT_RETRY_BACKOFF_MULTIPLIER,
        "exit_retry_max_cooldown_s": EXIT_RETRY_MAX_COOLDOWN_S,
        "exit_max_attempts": EXIT_MAX_ATTEMPTS,
        "flatten_before_close_s": FLATTEN_BEFORE_CLOSE_S,
        "entry_safety_buffer_s": ENTRY_SAFETY_BUFFER_S,
        "approval_skew_s": APPROVAL_SKEW_S,
        "min_remaining_for_entry_s": MIN_REMAINING_FOR_ENTRY_S,
        "max_hold_s_floor": MAX_HOLD_S_FLOOR,
        "qty_step": str(QTY_STEP),
        "money_step": str(MONEY_STEP),
        "min_tradable_qty": str(MIN_TRADABLE_QTY),
        "max_buy_collateral": str(MAX_BUY_COLLATERAL),
        "strategy_id": STRATEGY_ID,
        "market_family": MARKET_FAMILY,
        "max_windows": MAX_WINDOWS,
        "source_module": "tyrex_pm.runtime.r7_lifecycle_policy",
    }
