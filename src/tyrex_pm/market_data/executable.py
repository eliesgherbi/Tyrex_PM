"""Pure executable book views (Decimal)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.core.snapshots import BookLevel, BookSnapshot


@dataclass(frozen=True, kw_only=True)
class ExecutableQuote:
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid: Decimal | None
    spread: Decimal | None
    bid_size_at_touch: Decimal
    ask_size_at_touch: Decimal


def book_quote(book: BookSnapshot | None) -> ExecutableQuote:
    if book is None or (not book.bids and not book.asks):
        return ExecutableQuote(
            best_bid=None,
            best_ask=None,
            mid=None,
            spread=None,
            bid_size_at_touch=Decimal("0"),
            ask_size_at_touch=Decimal("0"),
        )
    best_bid = book.best_bid.price if book.best_bid else None
    best_ask = book.best_ask.price if book.best_ask else None
    mid = None
    spread = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
    return ExecutableQuote(
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        bid_size_at_touch=book.best_bid.quantity if book.best_bid else Decimal("0"),
        ask_size_at_touch=book.best_ask.quantity if book.best_ask else Decimal("0"),
    )


@dataclass(frozen=True, kw_only=True)
class VwapResult:
    vwap: Decimal | None
    filled_qty: Decimal
    requested_qty: Decimal
    sufficient: bool


def executable_vwap(
    levels: tuple[BookLevel, ...],
    quantity: Decimal,
    *,
    side: str,
) -> VwapResult:
    """VWAP walking asks for BUY, bids for SELL."""
    if quantity <= 0:
        raise ValueError("quantity must be > 0")
    remaining = quantity
    notional = Decimal("0")
    filled = Decimal("0")
    for level in levels:
        if remaining <= 0:
            break
        take = min(remaining, level.quantity)
        notional += take * level.price
        filled += take
        remaining -= take
    if filled == 0:
        return VwapResult(
            vwap=None,
            filled_qty=Decimal("0"),
            requested_qty=quantity,
            sufficient=False,
        )
    return VwapResult(
        vwap=notional / filled,
        filled_qty=filled,
        requested_qty=quantity,
        sufficient=filled >= quantity,
    )
