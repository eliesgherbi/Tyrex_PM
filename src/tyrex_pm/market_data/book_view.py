"""Immutable strategy-facing book views (shares at book levels; USDC notional separate)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.market_data.book_health import (
    ConnectionHealth,
    SideLiquidity,
    SyncHealth,
)
from tyrex_pm.market_data.executable import ExecutableQuote, executable_vwap


@dataclass(frozen=True, kw_only=True)
class ExecutableBookQuote:
    """Share-denominated executable walk result for one token/side.

    ``requested_quantity`` / ``filled_quantity`` / ``available_depth`` / ``shortfall``
    are **outcome shares**. Prices are USDC-per-share. Callers convert from
    ``target_notional`` (USDC) separately.
    """

    binding_id: str
    token_id: str
    side: str  # BUY walks asks; SELL walks bids
    requested_quantity: Decimal
    filled_quantity: Decimal
    available_depth: Decimal
    shortfall: Decimal
    vwap: Decimal | None
    worst_price: Decimal | None
    book_version: int
    sync_health: SyncHealth
    ask_liquidity: SideLiquidity
    bid_liquidity: SideLiquidity


@dataclass(frozen=True, kw_only=True)
class LegBookSnapshot:
    """One leg inside an atomic BookView."""

    token_id: str
    instrument_id: InstrumentId
    book: BookSnapshot | None
    book_version: int
    venue_hash: str | None
    sync_health: SyncHealth
    connection_health: ConnectionHealth
    bid_liquidity: SideLiquidity
    ask_liquidity: SideLiquidity
    tick_size: Decimal | None
    min_order_size: Decimal | None
    source_ts: datetime | None
    receive_ts: datetime | None
    apply_ts: datetime | None
    apply_mono_ns: int | None
    data_age_ms: int | None
    quote: ExecutableQuote

    @property
    def ready_for_entry_ask(self) -> bool:
        return (
            self.sync_health is SyncHealth.READY
            and self.ask_liquidity is SideLiquidity.AVAILABLE
            and self.quote.best_ask is not None
        )


@dataclass(frozen=True, kw_only=True)
class BookView:
    """Atomic two-leg view for one binding; per-leg readiness remains independent."""

    binding_id: str
    role_epoch: int
    window_slug: str
    condition_id: str
    connection_epoch: int
    captured_at: datetime
    pair_version: tuple[int, int]
    up: LegBookSnapshot
    down: LegBookSnapshot

    def leg_book_view(self, *, up: bool):
        """Return a strategy LegBookView without importing strategies at module import."""
        from tyrex_pm.strategies.z_gap.valuations import LegBookView

        leg = self.up if up else self.down
        ask = leg.quote.best_ask
        bid = leg.quote.best_bid
        ready = False
        reason = None
        if leg.sync_health is SyncHealth.UNINITIALIZED:
            reason = "BOOK_UNAVAILABLE"
        elif leg.sync_health is SyncHealth.SYNCING:
            reason = "BOOK_SYNCING"
        elif leg.sync_health is SyncHealth.STALE:
            reason = "BOOK_STALE"
        elif leg.sync_health is SyncHealth.DESYNCED:
            reason = "BOOK_DESYNCED"
        elif leg.ask_liquidity is SideLiquidity.EXPLICITLY_EMPTY and ask is None:
            reason = "VENUE_ASK_EXPLICITLY_EMPTY"
            # Bid may still be usable for exits; entry readiness stays false.
            ready = bid is not None
        elif leg.bid_liquidity is SideLiquidity.EXPLICITLY_EMPTY and bid is None and ask is None:
            reason = "VENUE_BID_EXPLICITLY_EMPTY"
        elif ask is None and bid is None:
            reason = "BOOK_UNAVAILABLE"
        else:
            ready = ask is not None or bid is not None
            if leg.ask_liquidity is SideLiquidity.EXPLICITLY_EMPTY and ask is None:
                reason = "VENUE_ASK_EXPLICITLY_EMPTY"
            elif leg.bid_liquidity is SideLiquidity.EXPLICITLY_EMPTY and bid is None:
                reason = "VENUE_BID_EXPLICITLY_EMPTY"
        return LegBookView(
            ask=ask,
            ask_depth=leg.quote.ask_size_at_touch if ask is not None else None,
            bid=bid,
            bid_depth=leg.quote.bid_size_at_touch if bid is not None else None,
            ready=ready,
            reason_code=reason,
        )

    def executable_buy(
        self,
        *,
        up: bool,
        requested_shares: Decimal,
    ) -> ExecutableBookQuote:
        leg = self.up if up else self.down
        levels = () if leg.book is None else leg.book.asks
        walk = executable_vwap(levels, requested_shares, side="BUY")
        worst = None
        filled = Decimal("0")
        remaining = requested_shares
        for level in levels:
            if remaining <= 0:
                break
            take = min(remaining, level.quantity)
            filled += take
            remaining -= take
            worst = level.price
        available = sum((lv.quantity for lv in levels), Decimal("0"))
        return ExecutableBookQuote(
            binding_id=self.binding_id,
            token_id=leg.token_id,
            side="BUY",
            requested_quantity=requested_shares,
            filled_quantity=walk.filled_qty,
            available_depth=available,
            shortfall=max(Decimal("0"), requested_shares - walk.filled_qty),
            vwap=walk.vwap,
            worst_price=worst,
            book_version=leg.book_version,
            sync_health=leg.sync_health,
            ask_liquidity=leg.ask_liquidity,
            bid_liquidity=leg.bid_liquidity,
        )


def side_liquidity_from_book(book: BookSnapshot | None, *, side: str) -> SideLiquidity:
    if book is None:
        return SideLiquidity.UNKNOWN
    levels = book.bids if side == "BID" else book.asks
    if levels:
        return SideLiquidity.AVAILABLE
    return SideLiquidity.EXPLICITLY_EMPTY
