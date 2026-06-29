"""Shared, stale-aware market state (Phase 2 architecture_enhance).

One read source for order-book truth used by the ExecutionPlanner (Phase 3),
ProtectionEngine (Phase 4) and risk marks. Mutation is single-writer: only
``ingestion/market_stream.py`` (live) and ``venue/polymarket/book_snapshot.py``
(REST bootstrap/repair) call :meth:`MarketStateStore.apply_snapshot`. Every read
accessor is pure and tolerates missing tokens by returning ``None`` rather than
raising. Stale/missing data is never silently treated as reliable truth — callers
must consult :meth:`MarketStateStore.is_stale`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now

#: Default freshness window: a book older than this is considered stale.
DEFAULT_MAX_BOOK_AGE_S = 5.0


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class MarketBookSnapshot:
    """Immutable point-in-time book for one token.

    ``bids`` are sorted best-first (highest price first); ``asks`` best-first
    (lowest price first). ``ts`` is the wall-clock time the snapshot was applied.
    """

    token_id: TokenId
    bids: tuple[BookLevel, ...] = ()
    asks: tuple[BookLevel, ...] = ()
    ts: datetime = field(default_factory=utc_now)

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None


def _sorted_levels(levels: list[BookLevel], *, descending: bool) -> tuple[BookLevel, ...]:
    cleaned = [lv for lv in levels if lv.size > 0]
    cleaned.sort(key=lambda lv: lv.price, reverse=descending)
    return tuple(cleaned)


def make_snapshot(
    token_id: TokenId,
    *,
    bids: list[tuple[Decimal, Decimal]] | None = None,
    asks: list[tuple[Decimal, Decimal]] | None = None,
    ts: datetime | None = None,
) -> MarketBookSnapshot:
    """Helper: build a normalized (sorted, zero-size filtered) snapshot."""
    bid_levels = _sorted_levels([BookLevel(p, s) for p, s in (bids or [])], descending=True)
    ask_levels = _sorted_levels([BookLevel(p, s) for p, s in (asks or [])], descending=False)
    return MarketBookSnapshot(
        token_id=token_id,
        bids=bid_levels,
        asks=ask_levels,
        ts=ts or utc_now(),
    )


class MarketStateStore:
    """In-memory per-token order book with stale-aware read accessors."""

    def __init__(self, *, default_max_age_s: float = DEFAULT_MAX_BOOK_AGE_S) -> None:
        self._books: dict[TokenId, MarketBookSnapshot] = {}
        self._default_max_age_s = default_max_age_s

    # --- single writer -----------------------------------------------------
    def apply_snapshot(self, snapshot: MarketBookSnapshot) -> None:
        """Replace the book for ``snapshot.token_id`` (single-writer entry point)."""
        self._books[snapshot.token_id] = snapshot

    # --- pure reads --------------------------------------------------------
    def snapshot(self, token_id: TokenId) -> MarketBookSnapshot | None:
        return self._books.get(token_id)

    def best_bid(self, token_id: TokenId) -> Decimal | None:
        book = self._books.get(token_id)
        return book.best_bid if book is not None else None

    def best_ask(self, token_id: TokenId) -> Decimal | None:
        book = self._books.get(token_id)
        return book.best_ask if book is not None else None

    def spread(self, token_id: TokenId) -> Decimal | None:
        bid = self.best_bid(token_id)
        ask = self.best_ask(token_id)
        if bid is None or ask is None:
            return None
        return ask - bid

    def mid(self, token_id: TokenId) -> Decimal | None:
        bid = self.best_bid(token_id)
        ask = self.best_ask(token_id)
        if bid is None or ask is None:
            return None
        return (bid + ask) / Decimal("2")

    def last_update_ts(self, token_id: TokenId) -> datetime | None:
        book = self._books.get(token_id)
        return book.ts if book is not None else None

    def is_stale(self, token_id: TokenId, *, max_age_s: float | None = None, now: datetime | None = None) -> bool:
        """True when the book is missing or older than ``max_age_s``.

        Fail-closed: a token with no book is always stale. ``now`` is injectable
        for deterministic tests.
        """
        book = self._books.get(token_id)
        if book is None:
            return True
        age_limit = self._default_max_age_s if max_age_s is None else max_age_s
        ref = now or utc_now()
        age_s = (ref - book.ts).total_seconds()
        return age_s > age_limit

    def estimate_fill_price(
        self,
        token_id: TokenId,
        side: Side,
        size: Decimal,
    ) -> Decimal | None:
        """Size-weighted average fill price walking the resting book.

        BUY consumes asks (cheapest first); SELL consumes bids (highest first).
        Returns the VWAP over ``min(size, available_depth)``; ``None`` when the
        book is missing or the relevant side is empty / size non-positive.
        """
        if size <= 0:
            return None
        book = self._books.get(token_id)
        if book is None:
            return None
        levels = book.asks if side == Side.BUY else book.bids
        if not levels:
            return None
        remaining = size
        cost = Decimal("0")
        filled = Decimal("0")
        for lv in levels:
            take = lv.size if lv.size < remaining else remaining
            cost += take * lv.price
            filled += take
            remaining -= take
            if remaining <= 0:
                break
        if filled <= 0:
            return None
        return cost / filled

    def estimate_slippage(
        self,
        token_id: TokenId,
        side: Side,
        size: Decimal,
    ) -> Decimal | None:
        """Absolute price slippage between the VWAP fill and the touch price.

        ``None`` when a fill price or the relevant touch is unavailable.
        """
        vwap = self.estimate_fill_price(token_id, side, size)
        if vwap is None:
            return None
        touch = self.best_ask(token_id) if side == Side.BUY else self.best_bid(token_id)
        if touch is None:
            return None
        return abs(vwap - touch)
