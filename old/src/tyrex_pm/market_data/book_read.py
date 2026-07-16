"""Generic best-bid/ask book reads for strategy evaluation (A0.5)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.market_store import MarketStateStore
from tyrex_pm.runtime.time_authority import TimeAuthority

QUALITY_OK = "ok"
QUALITY_STALE = "stale"
QUALITY_MISSING = "missing"


@dataclass(frozen=True)
class LegBookQuote:
    token_id: str
    bid: Decimal | None
    ask: Decimal | None
    stale: bool
    book_age_ms: int | None
    spread: Decimal | None
    quality_status: str
    book_update_ts: str | None = None


@dataclass(frozen=True)
class PairBookSnapshot:
    """UP (yes) and DOWN (no) leg quotes for binary markets."""

    up: LegBookQuote
    down: LegBookQuote

    def to_summary(self) -> dict[str, object]:
        return {
            "ask_up": str(self.up.ask) if self.up.ask is not None else None,
            "ask_down": str(self.down.ask) if self.down.ask is not None else None,
            "bid_up": str(self.up.bid) if self.up.bid is not None else None,
            "bid_down": str(self.down.bid) if self.down.bid is not None else None,
            "book_age_ms_up": self.up.book_age_ms,
            "book_age_ms_down": self.down.book_age_ms,
            "spread_up": str(self.up.spread) if self.up.spread is not None else None,
            "spread_down": str(self.down.spread) if self.down.spread is not None else None,
            "quality_up": self.up.quality_status,
            "quality_down": self.down.quality_status,
        }


def read_leg_book(
    market_state: MarketStateStore | None,
    token_id: str | TokenId,
    *,
    max_book_age_s: float,
    now: datetime | None = None,
    time_authority: TimeAuthority | None = None,
) -> LegBookQuote:
    tid = str(token_id)
    if market_state is None:
        return LegBookQuote(
            token_id=tid,
            bid=None,
            ask=None,
            stale=True,
            book_age_ms=None,
            spread=None,
            quality_status=QUALITY_MISSING,
        )
    snap = market_state.snapshot(TokenId(tid))
    if snap is None:
        return LegBookQuote(
            token_id=tid,
            bid=None,
            ask=None,
            stale=True,
            book_age_ms=None,
            spread=None,
            quality_status=QUALITY_MISSING,
        )
    stale = market_state.is_stale(TokenId(tid), max_age_s=max_book_age_s, now=now)
    ref_now = (
        now
        if now is not None
        else (
            time_authority.corrected_now()
            if time_authority is not None
            else utc_now()
        )
    )
    age_ms = int(max(0.0, (ref_now - snap.ts).total_seconds()) * 1000)
    spread: Decimal | None = None
    if snap.best_bid is not None and snap.best_ask is not None:
        spread = snap.best_ask - snap.best_bid
    quality = QUALITY_STALE if stale else QUALITY_OK
    if snap.best_ask is None and snap.best_bid is None:
        quality = QUALITY_MISSING
    return LegBookQuote(
        token_id=tid,
        bid=snap.best_bid,
        ask=snap.best_ask,
        stale=stale,
        book_age_ms=age_ms,
        spread=spread,
        quality_status=quality,
        book_update_ts=snap.ts.isoformat(),
    )


def read_pair_books(
    market_state: MarketStateStore | None,
    *,
    yes_token_id: str,
    no_token_id: str,
    max_book_age_s: float,
    now: datetime | None = None,
    time_authority: TimeAuthority | None = None,
) -> PairBookSnapshot:
    """Read UP (yes) and DOWN (no) books."""
    return PairBookSnapshot(
        up=read_leg_book(
            market_state,
            yes_token_id,
            max_book_age_s=max_book_age_s,
            now=now,
            time_authority=time_authority,
        ),
        down=read_leg_book(
            market_state,
            no_token_id,
            max_book_age_s=max_book_age_s,
            now=now,
            time_authority=time_authority,
        ),
    )
