"""Shared, stale-aware market state (Phase 2 architecture_enhance + v2 metadata).

One read source for order-book truth used by the ExecutionPlanner (Phase 3),
ProtectionEngine (Phase 4) and risk marks. Mutation is single-writer: only
``ingestion/market_stream.py`` (live) and ``venue/polymarket/book_snapshot.py``
(REST bootstrap/repair) call :meth:`MarketStateStore.apply_snapshot`. Every read
accessor is pure and tolerates missing tokens by returning ``None`` rather than
raising. Stale/missing data is never silently treated as reliable truth — callers
must consult :meth:`MarketStateStore.is_stale`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.models import (
    BookLevel,
    BookSource,
    MarketStateSnapshot,
    PairMarketSnapshot,
    SourceQuality,
)

__all__ = [
    "BookLevel",
    "DEFAULT_MAX_BOOK_AGE_S",
    "MarketBookSnapshot",
    "MarketStateStore",
    "make_snapshot",
    "parse_exchange_ts",
]

#: Default freshness window: a book older than this is considered stale.
DEFAULT_MAX_BOOK_AGE_S = 5.0


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


@dataclass
class _TokenMeta:
    source: str = BookSource.REST_POLL
    source_quality: str = SourceQuality.REST_POLL
    received_ts: datetime = field(default_factory=utc_now)
    exchange_ts: datetime | None = None
    sequence: int | None = None
    book_hash: str | None = None
    reconnect_gap: bool = False
    snapshot_id: str = ""


def _sorted_levels(levels: list[BookLevel], *, descending: bool) -> tuple[BookLevel, ...]:
    cleaned = [lv for lv in levels if lv.size > 0]
    cleaned.sort(key=lambda lv: lv.price, reverse=descending)
    return tuple(cleaned)


def _cap_levels(levels: tuple[BookLevel, ...], top_n: int | None) -> tuple[BookLevel, ...]:
    if top_n is None or top_n <= 0:
        return levels
    return levels[:top_n]


def _source_quality_for(source: str) -> str:
    if source == BookSource.WEBSOCKET:
        return SourceQuality.WS_PRIMARY
    if source == BookSource.REST_BOOTSTRAP:
        return SourceQuality.REST_BOOTSTRAP
    if source == BookSource.REST_RECOVERY:
        return SourceQuality.REST_RECOVERY
    return SourceQuality.REST_POLL


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

    def __init__(
        self,
        *,
        default_max_age_s: float = DEFAULT_MAX_BOOK_AGE_S,
        store_top_n_levels: int | None = 5,
    ) -> None:
        self._books: dict[TokenId, MarketBookSnapshot] = {}
        self._meta: dict[TokenId, _TokenMeta] = {}
        self._default_max_age_s = default_max_age_s
        self._store_top_n_levels = store_top_n_levels
        self._on_token_update: Callable[[TokenId], None] | None = None

    def set_on_token_update(self, callback: Callable[[TokenId], None] | None) -> None:
        """Optional callback invoked after each ``apply_book`` (authoritative store only)."""
        self._on_token_update = callback

    # --- single writer -----------------------------------------------------
    def apply_snapshot(self, snapshot: MarketBookSnapshot) -> None:
        """Replace the book for ``snapshot.token_id`` (legacy single-writer entry point)."""
        self.apply_book(
            snapshot.token_id,
            list(snapshot.bids),
            list(snapshot.asks),
            received_ts=snapshot.ts,
            source=BookSource.REST_POLL,
        )

    def apply_book(
        self,
        token_id: TokenId,
        bids: list[BookLevel] | tuple[BookLevel, ...],
        asks: list[BookLevel] | tuple[BookLevel, ...],
        *,
        source: str = BookSource.REST_POLL,
        source_quality: str | None = None,
        received_ts: datetime | None = None,
        exchange_ts: datetime | None = None,
        sequence: int | None = None,
        book_hash: str | None = None,
        reconnect_gap: bool | None = None,
    ) -> str:
        """Apply a normalized book update and return the new ``snapshot_id``."""
        bid_tuple = _cap_levels(
            _sorted_levels(list(bids), descending=True),
            self._store_top_n_levels,
        )
        ask_tuple = _cap_levels(
            _sorted_levels(list(asks), descending=False),
            self._store_top_n_levels,
        )
        ref_ts = received_ts or utc_now()
        snap = MarketBookSnapshot(token_id=token_id, bids=bid_tuple, asks=ask_tuple, ts=ref_ts)
        prev = self._meta.get(token_id)
        gap = prev.reconnect_gap if prev is not None and reconnect_gap is None else bool(reconnect_gap)
        new_quality = source_quality or _source_quality_for(source)
        if (
            prev is not None
            and prev.source_quality == SourceQuality.WS_PRIMARY
            and new_quality != SourceQuality.WS_PRIMARY
            and source != BookSource.WEBSOCKET
            and not gap
        ):
            return prev.snapshot_id
        self._books[token_id] = snap
        snapshot_id = str(uuid.uuid4())
        self._meta[token_id] = _TokenMeta(
            source=source,
            source_quality=source_quality or _source_quality_for(source),
            received_ts=ref_ts,
            exchange_ts=exchange_ts,
            sequence=sequence,
            book_hash=book_hash,
            reconnect_gap=gap,
            snapshot_id=snapshot_id,
        )
        if self._on_token_update is not None:
            self._on_token_update(token_id)
        return snapshot_id

    # --- v2 metadata -----------------------------------------------------
    def last_sequence(self, token_id: TokenId) -> int | None:
        meta = self._meta.get(token_id)
        return meta.sequence if meta is not None else None

    def last_book_hash(self, token_id: TokenId) -> str | None:
        meta = self._meta.get(token_id)
        return meta.book_hash if meta is not None else None

    def reconnect_gap(self, token_id: TokenId) -> bool:
        meta = self._meta.get(token_id)
        return meta.reconnect_gap if meta is not None else False

    def set_reconnect_gap(self, token_id: TokenId, value: bool) -> None:
        meta = self._meta.get(token_id)
        if meta is None:
            self._meta[token_id] = _TokenMeta(reconnect_gap=value)
        else:
            meta.reconnect_gap = value

    def book_age_ms(self, token_id: TokenId, *, now: datetime | None = None) -> int | None:
        meta = self._meta.get(token_id)
        if meta is None:
            return None
        ref = now or utc_now()
        return int(max(0.0, (ref - meta.received_ts).total_seconds()) * 1000)

    def capture(self, token_id: TokenId, *, now: datetime | None = None) -> MarketStateSnapshot | None:
        book = self._books.get(token_id)
        meta = self._meta.get(token_id)
        if book is None or meta is None:
            return None
        bid = book.best_bid
        ask = book.best_ask
        spread = (ask - bid) if bid is not None and ask is not None else None
        mid = (bid + ask) / Decimal("2") if bid is not None and ask is not None else None
        age = self.book_age_ms(token_id, now=now) or 0
        return MarketStateSnapshot(
            token_id=token_id,
            bids=book.bids,
            asks=book.asks,
            best_bid=bid,
            best_ask=ask,
            best_bid_size=book.bids[0].size if book.bids else None,
            best_ask_size=book.asks[0].size if book.asks else None,
            spread=spread,
            mid=mid,
            received_ts=meta.received_ts,
            exchange_ts=meta.exchange_ts,
            book_age_ms=age,
            source=meta.source,
            source_quality=meta.source_quality,
            sequence=meta.sequence,
            book_hash=meta.book_hash,
            snapshot_id=meta.snapshot_id,
            reconnect_gap=meta.reconnect_gap,
        )

    def capture_pair(
        self,
        yes_token_id: TokenId,
        no_token_id: TokenId,
        pair_id: str,
        *,
        now: datetime | None = None,
    ) -> PairMarketSnapshot | None:
        yes = self.capture(yes_token_id, now=now)
        no = self.capture(no_token_id, now=now)
        if yes is None or no is None:
            return None
        captured_at = max(yes.received_ts, no.received_ts)
        return PairMarketSnapshot(
            pair_id=pair_id,
            yes=yes,
            no=no,
            pair_snapshot_id=str(uuid.uuid4()),
            captured_at=captured_at,
        )

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
        meta = self._meta.get(token_id)
        if meta is not None:
            return meta.received_ts
        book = self._books.get(token_id)
        return book.ts if book is not None else None

    def is_stale(self, token_id: TokenId, *, max_age_s: float | None = None, now: datetime | None = None) -> bool:
        """True when the book is missing or older than ``max_age_s``.

        Fail-closed: a token with no book is always stale. ``now`` is injectable
        for deterministic tests.
        """
        meta = self._meta.get(token_id)
        book = self._books.get(token_id)
        if book is None or meta is None:
            return True
        age_limit = self._default_max_age_s if max_age_s is None else max_age_s
        ref = now or utc_now()
        age_s = (ref - meta.received_ts).total_seconds()
        return age_s > age_limit

    def estimate_fill_price(
        self,
        token_id: TokenId,
        side: Side,
        size: Decimal,
    ) -> Decimal | None:
        """Size-weighted average fill price walking the resting book."""
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
        """Absolute price slippage between the VWAP fill and the touch price."""
        vwap = self.estimate_fill_price(token_id, side, size)
        if vwap is None:
            return None
        touch = self.best_ask(token_id) if side == Side.BUY else self.best_bid(token_id)
        if touch is None:
            return None
        return abs(vwap - touch)


def parse_exchange_ts(raw: object) -> datetime | None:
    """Parse Polymarket WS ``timestamp`` (ms string/int) to UTC datetime."""
    if raw is None:
        return None
    try:
        ms = int(str(raw))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
