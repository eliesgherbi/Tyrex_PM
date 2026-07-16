"""ExecutableBookView depth-at-size tests."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.market_data.executable_book import ExecutableBookView
from tyrex_pm.market_data.models import BookLevel, BookSource
from tyrex_pm.state.market_store import MarketStateStore

TOKEN = TokenId("tok-eb")


def _capture(bids, asks):
    store = MarketStateStore()
    store.apply_book(TOKEN, bids, asks, source=BookSource.WEBSOCKET, received_ts=utc_now())
    snap = store.capture(TOKEN, now=utc_now())
    assert snap is not None
    return snap


def test_thin_book_worst_worse_than_touch_sell() -> None:
    snap = _capture(
        [BookLevel(Decimal("0.50"), Decimal("5")), BookLevel(Decimal("0.45"), Decimal("100"))],
        [BookLevel(Decimal("0.55"), Decimal("100"))],
    )
    view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=Decimal("50"))
    assert view.touch_price == Decimal("0.50")
    assert view.worst_price_to_fill == Decimal("0.45")
    assert view.worst_price_to_fill < view.touch_price


def test_deep_book_worst_equals_touch() -> None:
    snap = _capture(
        [BookLevel(Decimal("0.49"), Decimal("100"))],
        [BookLevel(Decimal("0.51"), Decimal("100"))],
    )
    view = ExecutableBookView.from_snapshot(snap, side=Side.SELL, size=Decimal("50"))
    assert view.touch_price == Decimal("0.49")
    assert view.worst_price_to_fill == Decimal("0.49")


def test_buy_side_uses_asks() -> None:
    snap = _capture(
        [BookLevel(Decimal("0.48"), Decimal("100"))],
        [BookLevel(Decimal("0.52"), Decimal("5")), BookLevel(Decimal("0.55"), Decimal("100"))],
    )
    view = ExecutableBookView.from_snapshot(snap, side=Side.BUY, size=Decimal("50"))
    assert view.touch_price == Decimal("0.52")
    assert view.worst_price_to_fill == Decimal("0.55")
    assert view.sweep_vwap is not None
