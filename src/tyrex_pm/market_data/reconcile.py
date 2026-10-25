"""Causal venue-top reconciliation shared by live and recorded Polymarket books.

Only deletes levels contradicted by this message. It never invents depth. Matching
prices does not verify quantities or continuity. Locked books are unavailable in v1.
"""
from decimal import Decimal

LEGACY = "legacy"
VENUE_TOP_V1 = "venue_top_v1"
BOOK_POLICIES = frozenset({LEGACY, VENUE_TOP_V1})


def is_crossed(bids, asks) -> bool:
    """Best bid at or above best ask: a matching venue never holds such a book.

    The one rule for live books (market_data.book_store) and rebuilt recorded books
    (data_warehouse.reconstruction.books). Under venue_top_v1 neither a locked nor a
    crossed book occurred in an hour of recorded BTC 5-minute books (6.1 million checks).
    """
    return bool(bids) and bool(asks) and max(bids) >= min(asks)


def reconcile_top(bids: dict, asks: dict, best_bid=None, best_ask=None) -> int:
    reports = (best_bid, best_ask)
    for value in reports:
        if value is not None and (not value.is_finite() or not Decimal(0) <= value <= Decimal(1)):
            raise ValueError("invalid reported venue best")
    if all(v is not None for v in reports) and best_bid >= best_ask:
        raise ValueError("venue crossed or locked best")
    removed = 0
    for levels, reported, bid in ((bids, best_bid, True), (asks, best_ask, False)):
        if reported is None:
            continue  # Missing fields give structural validation, never a verified top.
        empty = reported == (Decimal(0) if bid else Decimal(1))
        obsolete = [p for p in levels if empty or (p > reported if bid else p < reported)]
        for price in obsolete:
            del levels[price]
        removed += len(obsolete)
        top = (max(levels) if bid else min(levels)) if levels else None
        if top != (None if empty else reported):
            raise ValueError("reported venue best absent from reconstructed depth")
    if bids and asks and max(bids) >= min(asks):
        raise ValueError("reconstructed book crossed or locked")
    return removed
