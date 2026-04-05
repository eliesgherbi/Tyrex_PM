"""C3 depth clip: single-level top-of-book size cap."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.execution.c3_book_top import BookTop


@dataclass(frozen=True, slots=True)
class DepthClipResult:
    quantity: float
    clipped: bool
    visible_liquidity: float | None
    detail: str


def clip_to_book_depth(
    *,
    side: str,
    quantity: float,
    book: BookTop,
    utilization_cap: float,
) -> DepthClipResult:
    """Clip ``quantity`` to ``utilization_cap`` × top-of-book size on the aggressive side."""
    cap = float(utilization_cap)
    if cap <= 0 or cap > 1.0 + 1e-9:
        return DepthClipResult(quantity, False, None, "invalid_cap")
    side_u = side.upper()
    if side_u == "BUY":
        vis = book.best_ask_size
        if vis is None or vis <= 0:
            return DepthClipResult(quantity, False, vis, "no_ask_size")
        max_q = cap * float(vis)
    elif side_u == "SELL":
        vis = book.best_bid_size
        if vis is None or vis <= 0:
            return DepthClipResult(quantity, False, vis, "no_bid_size")
        max_q = cap * float(vis)
    else:
        return DepthClipResult(quantity, False, None, "unknown_side")
    out = min(float(quantity), max_q)
    clipped = out + 1e-12 < float(quantity)
    return DepthClipResult(
        quantity=out,
        clipped=clipped,
        visible_liquidity=float(vis) if vis is not None else None,
        detail="ok",
    )
