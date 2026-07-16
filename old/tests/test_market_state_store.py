"""Phase 2 (architecture_enhance): MarketStateStore unit tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.core.time import utc_now
from tyrex_pm.state.market_store import MarketStateStore, make_snapshot

TOKEN = TokenId("tok-mkt")


def _store_with_book() -> MarketStateStore:
    store = MarketStateStore(default_max_age_s=5.0)
    store.apply_snapshot(
        make_snapshot(
            TOKEN,
            bids=[(Decimal("0.49"), Decimal("100")), (Decimal("0.48"), Decimal("200"))],
            asks=[(Decimal("0.51"), Decimal("100")), (Decimal("0.52"), Decimal("200"))],
        )
    )
    return store


def test_market_store_best_bid_ask_update() -> None:
    store = _store_with_book()
    assert store.best_bid(TOKEN) == Decimal("0.49")
    assert store.best_ask(TOKEN) == Decimal("0.51")
    # Re-apply moves the touch.
    store.apply_snapshot(
        make_snapshot(TOKEN, bids=[(Decimal("0.50"), Decimal("10"))], asks=[(Decimal("0.53"), Decimal("10"))])
    )
    assert store.best_bid(TOKEN) == Decimal("0.50")
    assert store.best_ask(TOKEN) == Decimal("0.53")


def test_market_store_spread_and_mid() -> None:
    store = _store_with_book()
    assert store.spread(TOKEN) == Decimal("0.02")
    assert store.mid(TOKEN) == Decimal("0.50")


def test_market_store_stale_detection() -> None:
    store = MarketStateStore(default_max_age_s=5.0)
    old = utc_now() - timedelta(seconds=30)
    store.apply_snapshot(make_snapshot(TOKEN, bids=[(Decimal("0.49"), Decimal("1"))], asks=[(Decimal("0.51"), Decimal("1"))], ts=old))
    assert store.is_stale(TOKEN) is True
    fresh = utc_now()
    store.apply_snapshot(make_snapshot(TOKEN, bids=[(Decimal("0.49"), Decimal("1"))], asks=[(Decimal("0.51"), Decimal("1"))], ts=fresh))
    assert store.is_stale(TOKEN) is False
    assert store.is_stale(TOKEN, max_age_s=0.0, now=fresh + timedelta(seconds=1)) is True


def test_estimate_fill_price_walks_book() -> None:
    store = _store_with_book()
    # BUY 150 shares: 100 @ 0.51 + 50 @ 0.52 = 51 + 26 = 77 / 150
    vwap = store.estimate_fill_price(TOKEN, Side.BUY, Decimal("150"))
    assert vwap == (Decimal("100") * Decimal("0.51") + Decimal("50") * Decimal("0.52")) / Decimal("150")
    # SELL 100 shares hits the top bid only.
    assert store.estimate_fill_price(TOKEN, Side.SELL, Decimal("100")) == Decimal("0.49")


def test_estimate_slippage_thin_book() -> None:
    store = _store_with_book()
    # BUY 150 walks into 0.52 → slippage vs 0.51 touch is positive.
    slip = store.estimate_slippage(TOKEN, Side.BUY, Decimal("150"))
    assert slip is not None and slip > 0
    # A size resting fully at the touch has zero slippage.
    assert store.estimate_slippage(TOKEN, Side.BUY, Decimal("100")) == Decimal("0")


def test_missing_token_returns_none_not_error() -> None:
    store = MarketStateStore()
    missing = TokenId("nope")
    assert store.best_bid(missing) is None
    assert store.best_ask(missing) is None
    assert store.spread(missing) is None
    assert store.mid(missing) is None
    assert store.last_update_ts(missing) is None
    assert store.estimate_fill_price(missing, Side.BUY, Decimal("10")) is None
    assert store.estimate_slippage(missing, Side.SELL, Decimal("10")) is None
    assert store.is_stale(missing) is True  # missing == stale (fail closed)


def test_single_writer_only() -> None:
    # The only public mutator is apply_snapshot; all other public methods are reads.
    store = MarketStateStore()
    mutators = [
        name
        for name in dir(store)
        if not name.startswith("_")
        and callable(getattr(store, name))
        and any(name.startswith(p) for p in ("set_", "update_", "upsert_", "put_", "add_", "remove_", "delete_"))
    ]
    assert mutators == ["set_on_token_update", "set_reconnect_gap"], f"unexpected mutator methods: {mutators}"


def test_ws_primary_not_regressed_by_rest_bootstrap() -> None:
    from tyrex_pm.market_data.models import BookLevel, BookSource, SourceQuality

    store = MarketStateStore()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("100"))],
        [BookLevel(Decimal("0.51"), Decimal("100"))],
        source=BookSource.WEBSOCKET,
        source_quality=SourceQuality.WS_PRIMARY,
    )
    cap = store.capture(TOKEN)
    assert cap is not None and cap.source_quality == SourceQuality.WS_PRIMARY
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.10"), Decimal("100"))],
        [BookLevel(Decimal("0.90"), Decimal("100"))],
        source=BookSource.REST_BOOTSTRAP,
    )
    cap2 = store.capture(TOKEN)
    assert cap2 is not None
    assert cap2.source_quality == SourceQuality.WS_PRIMARY
    assert store.best_bid(TOKEN) == Decimal("0.49")
