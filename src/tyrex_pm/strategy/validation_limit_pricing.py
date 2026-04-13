"""
Validation-only **marketable limit** pricing (Scenario A harness).

Chooses limit prices intended to **cross the spread quickly** using top-of-book when available,
with tick bumps and slippage caps so prices stay bounded vs a **reference** (guru trade price
or the entry fill’s limit). Production copy strategy does not import this module.

All Polymarket outcome prices are treated as lying in ``[tick, 1 - tick]`` (complement
token is separate).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from tyrex_pm.execution.c3_book_top import BookTop


@dataclass(frozen=True, slots=True)
class ValidationAggressiveQuote:
    """Inputs/outputs for operator logs and ``bot_sell_validate`` facts."""

    limit_price: float
    reference_price: float
    tick_size: float
    side: str  # BUY | SELL
    best_bid: float | None
    best_ask: float | None
    book_source: str
    anchor_description: str
    aggression_ticks: int
    clamp_note: str

    def as_fact_payload(self) -> dict:
        return {
            "limit_price": float(self.limit_price),
            "reference_price": float(self.reference_price),
            "tick_size": float(self.tick_size),
            "side": self.side,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "book_source": self.book_source,
            "anchor_description": self.anchor_description,
            "aggression_ticks": int(self.aggression_ticks),
            "clamp_note": self.clamp_note,
        }


def _ceil_tick(px: float, tick: float) -> float:
    if tick <= 0.0:
        return px
    n = math.ceil(px / tick - 1e-12)
    return round(min(1.0 - tick, n * tick), 12)


def _floor_tick(px: float, tick: float) -> float:
    if tick <= 0.0:
        return px
    n = math.floor(px / tick + 1e-12)
    return round(max(tick, n * tick), 12)


def _slip_high(ref: float, max_slip_frac: float) -> float:
    if ref > 1e-9:
        return min(1.0, float(ref) * (1.0 + float(max_slip_frac)))
    return 1.0


def _slip_low(ref: float, max_slip_frac: float) -> float:
    if ref > 1e-9:
        return max(0.0, float(ref) * (1.0 - float(max_slip_frac)))
    return 0.0


def aggressive_validation_buy_limit(
    *,
    reference_price: float,
    tick_size: float,
    book: BookTop,
    aggression_ticks: int,
    max_slippage_fraction: float,
) -> ValidationAggressiveQuote:
    """
    **Marketable BUY:** anchor at best ask when present (else one tick above best bid, else
    reference). Add ``aggression_ticks * tick`` upward, then clamp with slippage vs **reference**
    and ``[tick, 1 - tick]``.
    """
    ref = float(reference_price)
    tick = max(float(tick_size), 1e-6)
    n = max(0, int(aggression_ticks))
    bid, ask = book.best_bid, book.best_ask
    src = book.source

    if ask is not None:
        anchor = float(ask)
        desc = "best_ask"
    elif bid is not None:
        anchor = float(bid) + tick
        desc = "best_bid_plus_tick"
    else:
        anchor = ref
        desc = "reference_only"

    raw = anchor + n * tick
    cap = _slip_high(ref, max_slippage_fraction)
    clamp_note = ""
    if raw > cap + 1e-12:
        raw = cap
        clamp_note = "slippage_cap_high"
    hi = 1.0 - tick
    if raw > hi + 1e-12:
        raw = hi
        clamp_note = clamp_note + "|price_bound_high" if clamp_note else "price_bound_high"
    lo = tick
    if raw < lo - 1e-12:
        raw = lo
        clamp_note = clamp_note + "|price_bound_low" if clamp_note else "price_bound_low"

    px = _ceil_tick(raw, tick)
    return ValidationAggressiveQuote(
        limit_price=px,
        reference_price=ref,
        tick_size=tick,
        side="BUY",
        best_bid=bid,
        best_ask=ask,
        book_source=src,
        anchor_description=desc,
        aggression_ticks=n,
        clamp_note=clamp_note or "none",
    )


def aggressive_validation_sell_limit(
    *,
    reference_price: float,
    tick_size: float,
    book: BookTop,
    aggression_ticks: int,
    max_slippage_fraction: float,
) -> ValidationAggressiveQuote:
    """
    **Marketable SELL:** anchor at best bid when present (else one tick below best ask, else
    reference). Subtract ``aggression_ticks * tick``, then clamp with slippage vs **reference**
    and ``[tick, 1 - tick]``.
    """
    ref = float(reference_price)
    tick = max(float(tick_size), 1e-6)
    n = max(0, int(aggression_ticks))
    bid, ask = book.best_bid, book.best_ask
    src = book.source

    if bid is not None:
        anchor = float(bid)
        desc = "best_bid"
    elif ask is not None:
        anchor = float(ask) - tick
        desc = "best_ask_minus_tick"
    else:
        anchor = ref
        desc = "reference_only"

    raw = anchor - n * tick
    floor_s = _slip_low(ref, max_slippage_fraction)
    clamp_note = ""
    if raw < floor_s - 1e-12:
        raw = floor_s
        clamp_note = "slippage_cap_low"
    lo = tick
    if raw < lo - 1e-12:
        raw = lo
        clamp_note = clamp_note + "|price_bound_low" if clamp_note else "price_bound_low"
    hi = 1.0 - tick
    if raw > hi + 1e-12:
        raw = hi
        clamp_note = clamp_note + "|price_bound_high" if clamp_note else "price_bound_high"

    px = _floor_tick(raw, tick)
    return ValidationAggressiveQuote(
        limit_price=px,
        reference_price=ref,
        tick_size=tick,
        side="SELL",
        best_bid=bid,
        best_ask=ask,
        book_source=src,
        anchor_description=desc,
        aggression_ticks=n,
        clamp_note=clamp_note or "none",
    )


__all__ = [
    "ValidationAggressiveQuote",
    "aggressive_validation_buy_limit",
    "aggressive_validation_sell_limit",
]
