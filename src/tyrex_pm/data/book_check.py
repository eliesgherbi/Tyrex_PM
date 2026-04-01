"""Public CLOB order book checks (no auth)."""

from __future__ import annotations

from typing import Any


def summarize_book_sides(bids: list[Any] | None, asks: list[Any] | None) -> tuple[str, str]:
    """
    Return (status, detail) for milestone reporting.
    status: ok_liquidity | empty_book
    """
    nb = len(bids or [])
    na = len(asks or [])
    if nb == 0 and na == 0:
        return "empty_book", "no_bids_no_asks"
    return "ok_liquidity", f"bids={nb}_asks={na}"


def tick_size_matches_instrument(
    instrument_tick: str | float,
    clob_tick: str | float | None,
) -> bool:
    """Best-effort consistency check between Nautilus instrument and CLOB summary."""
    if clob_tick is None:
        return True
    return str(instrument_tick) == str(clob_tick)
