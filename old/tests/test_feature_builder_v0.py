"""FeatureBuilder v0 tests (Phase 2 M5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import TokenId
from tyrex_pm.market_data.features import FeatureBuilder
from tyrex_pm.market_data.models import BookSource, MarketStateSnapshot
from tyrex_pm.state.market_store import BookLevel

TOKEN = TokenId("tok-feat")


def _snap(**kwargs) -> MarketStateSnapshot:
    defaults = dict(
        token_id=TOKEN,
        bids=(BookLevel(Decimal("0.48"), Decimal("100")), BookLevel(Decimal("0.47"), Decimal("50"))),
        asks=(BookLevel(Decimal("0.52"), Decimal("80")), BookLevel(Decimal("0.53"), Decimal("40"))),
        best_bid=Decimal("0.48"),
        best_ask=Decimal("0.52"),
        best_bid_size=Decimal("100"),
        best_ask_size=Decimal("80"),
        spread=Decimal("0.04"),
        mid=Decimal("0.50"),
        received_ts=datetime(2026, 6, 29, tzinfo=timezone.utc),
        exchange_ts=None,
        book_age_ms=100,
        source=BookSource.WEBSOCKET,
        source_quality="ws_primary",
        sequence=None,
        book_hash=None,
        snapshot_id="snap-1",
        reconnect_gap=False,
    )
    defaults.update(kwargs)
    return MarketStateSnapshot(**defaults)


def test_detinistic_features_from_fixed_snapshot() -> None:
    snap = _snap()
    f = FeatureBuilder.build(snap, size=Decimal("5"))
    assert f.snapshot_id == "snap-1"
    assert f.spread == Decimal("0.04")
    assert f.mid == Decimal("0.50")
    assert f.best_bid == Decimal("0.48")
    assert f.sweep_vwap_buy == Decimal("0.52")
    assert f.sweep_vwap_sell == Decimal("0.48")
    assert f.depth_at_size == Decimal("5")
    assert "sweep_vwap_buy" not in f.missing_fields


def test_missing_bid_ask_reports_missing_fields() -> None:
    snap = _snap(
        bids=(),
        asks=(),
        best_bid=None,
        best_ask=None,
        best_bid_size=None,
        best_ask_size=None,
        spread=None,
        mid=None,
    )
    f = FeatureBuilder.build(snap, size=Decimal("5"))
    assert "best_bid" in f.missing_fields
    assert "best_ask" in f.missing_fields
    assert "spread" in f.missing_fields
    assert "mid" in f.missing_fields
    assert f.sweep_vwap_buy is None
    assert f.sweep_vwap_sell is None


def test_no_advanced_fields_on_dataclass() -> None:
    forbidden = {"obi", "microprice", "btc_spot", "volatility", "aggressor_flow", "ml_score"}
    f = FeatureBuilder.build(_snap(), size=Decimal("5"))
    fields = set(f.__dataclass_fields__.keys())
    assert forbidden.isdisjoint(fields)


def test_build_from_store() -> None:
    from tyrex_pm.state.market_store import MarketStateStore

    store = MarketStateStore()
    store.apply_book(
        TOKEN,
        [BookLevel(Decimal("0.49"), Decimal("10"))],
        [BookLevel(Decimal("0.51"), Decimal("10"))],
        source=BookSource.WEBSOCKET,
    )
    f = FeatureBuilder.build_from_store(store, TOKEN, size=Decimal("5"))
    assert f is not None
    assert f.source == BookSource.WEBSOCKET
