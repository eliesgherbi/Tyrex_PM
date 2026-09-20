"""Venue-legal remaining size and known-dust classification for exits.

Pure functions so leftover flatten can be replayed from live number sets
without a second OMS or a live POST.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Sequence

from tyrex_pm.execution.evidence import ExecutionRole
from tyrex_pm.execution.session_state import ExecutionSessionState, KnownDustResidual


def trade_remaining_after_exits(state: ExecutionSessionState) -> Decimal:
    """Buy inventory not yet covered by a confirmed sell or a matched EXIT HWM."""
    covered = max(state.confirmed_sell_shares, state.exit_matched_hwm)
    return max(Decimal("0"), state.confirmed_buy_shares - covered)


def reserved_open_sell_shares(state: ExecutionSessionState) -> Decimal:
    """Venue formula term: Σ(openOrderSize − filledAmount) for session EXIT orders."""
    open_ids = set(state.open_venue_order_ids)
    reserved = Decimal("0")
    for order in state.orders.values():
        if order.role is not ExecutionRole.EXIT:
            continue
        if order.venue_order_id is None or order.venue_order_id not in open_ids:
            continue
        original = getattr(order.spec, "shares", None)
        if original is None:
            continue
        reserved += max(Decimal("0"), Decimal(str(original)) - order.cumulative_matched_hwm)
    return reserved


def remaining_exit_shares(state: ExecutionSessionState) -> Decimal:
    """Authoritative leftover before the next FAK sell. Never reuse the first-exit clip."""
    reserved = reserved_open_sell_shares(state)
    venue_remaining = max(Decimal("0"), state.sellable_shares - reserved)
    position_cap = max(Decimal("0"), state.confirmed_position_shares)
    if state.has_confirmed_buy:
        position_cap = min(position_cap, max(Decimal("0"), state.confirmed_trade_net_shares))
    remaining = min(position_cap, venue_remaining)
    hwm_remaining = trade_remaining_after_exits(state)
    if hwm_remaining > 0:
        remaining = min(remaining, hwm_remaining)
    return max(Decimal("0"), remaining)


def venue_legal_sell_shares(remaining: Decimal, increment: Decimal) -> Decimal:
    """Round remaining shares down to the SELL maker increment (typically 0.01)."""
    increment = Decimal(str(increment))
    remaining = Decimal(str(remaining))
    if increment <= 0:
        raise ValueError("sell_size_increment must be positive")
    if remaining <= 0:
        return Decimal("0")
    steps = (remaining / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


def can_post_exit(*, legal_shares: Decimal, min_order_size: Decimal | None) -> bool:
    """Fail closed when book/binding min size is missing."""
    if min_order_size is None:
        return False
    return legal_shares > 0 and legal_shares >= Decimal(str(min_order_size))


@dataclass(frozen=True)
class DustClassification:
    code: str


def classify_dust_leftover(
    remaining: Decimal,
    *,
    increment: Decimal,
    min_order_size: Decimal | None,
    last_bid: Decimal | None,
    dust_abs_shares: Decimal,
    dust_max_notional: Decimal,
) -> DustClassification | None:
    """Return a classification when leftover is known dust; None if tradable or unknown."""
    remaining = Decimal(str(remaining))
    if remaining <= 0:
        return None
    legal = venue_legal_sell_shares(remaining, increment)
    if can_post_exit(legal_shares=legal, min_order_size=min_order_size):
        return None
    if remaining <= Decimal(str(dust_abs_shares)):
        return DustClassification("below_dust_abs_shares")
    notional = remaining * Decimal(str(last_bid)) if last_bid is not None else remaining
    if notional <= Decimal(str(dust_max_notional)):
        if min_order_size is not None and remaining < Decimal(str(min_order_size)):
            return DustClassification("below_min_order_size")
        return DustClassification("below_dust_max_notional")
    return None


def has_ambiguous_exit(state: ExecutionSessionState) -> bool:
    return any(
        order.role is ExecutionRole.EXIT and order.ambiguous for order in state.orders.values()
    )


def should_wait_before_exit_post(state: ExecutionSessionState) -> bool:
    if state.open_venue_order_ids:
        return True
    return has_ambiguous_exit(state)


def is_exempt_prior_position_blocker(
    blocker: str,
    residuals: Sequence[KnownDustResidual],
) -> bool:
    """Known dust on a leftover token must not set PRIOR_SCOPE_NOT_CLEAR."""
    if not blocker.startswith("PRIOR_POSITION:"):
        return False
    token_id, sep, qty_text = blocker[len("PRIOR_POSITION:") :].rpartition(":")
    if not sep or not token_id:
        return False
    try:
        balance = Decimal(str(qty_text))
    except Exception:
        return False
    for residual in residuals:
        if residual.token_id != token_id:
            continue
        if balance <= residual.shares:
            return True
    return False
