"""C3 entry price guard vs guru reference (execution-quality, not risk)."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.execution.c3_book_top import BookTop


@dataclass(frozen=True, slots=True)
class EntryGuardResult:
    ok: bool
    detail: str = ""


def check_entry_guard(
    *,
    side: str,
    reference_price: float,
    book: BookTop,
    max_slippage_ticks: int,
    tick_size: float,
) -> EntryGuardResult:
    """If market worse than ``max_slippage_ticks`` × ``tick_size`` vs reference → skip."""
    if max_slippage_ticks <= 0:
        return EntryGuardResult(True, "disabled_ticks")
    tol = max(float(tick_size), 1e-12) * float(max_slippage_ticks)
    side_u = side.upper()
    if side_u == "BUY":
        if book.best_ask is None:
            return EntryGuardResult(False, "missing_best_ask")
        slip = float(book.best_ask) - float(reference_price)
        if slip > tol + 1e-12:
            return EntryGuardResult(
                False,
                f"ask={book.best_ask} ref={reference_price} slip={slip} tol={tol}",
            )
    elif side_u == "SELL":
        if book.best_bid is None:
            return EntryGuardResult(False, "missing_best_bid")
        slip = float(reference_price) - float(book.best_bid)
        if slip > tol + 1e-12:
            return EntryGuardResult(
                False,
                f"bid={book.best_bid} ref={reference_price} slip={slip} tol={tol}",
            )
    else:
        return EntryGuardResult(True, "unknown_side_skip")
    return EntryGuardResult(True, "ok")
