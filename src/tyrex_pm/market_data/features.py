"""FeatureBuilder v0 — basic book-derived fields only (Phase 2 M5).

Does not affect strategy decisions; consumed by reporting / future M7 snapshots.
"""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.core.enums import Side
from tyrex_pm.market_data.models import BasicFeatureSnapshot, BookLevel, MarketStateSnapshot, PairMarketSnapshot
from tyrex_pm.state.market_store import MarketStateStore


class FeatureBuilder:
    """Minimal deterministic feature extraction from captured store snapshots."""

    @staticmethod
    def build(
        snap: MarketStateSnapshot,
        *,
        size: Decimal,
        quality_verdict: str | None = None,
    ) -> BasicFeatureSnapshot:
        missing: list[str] = []
        if snap.best_bid is None:
            missing.append("best_bid")
        if snap.best_ask is None:
            missing.append("best_ask")
        if snap.spread is None:
            missing.append("spread")
        if snap.mid is None:
            missing.append("mid")

        depth_at_size = _depth_at_size(snap, size)
        if depth_at_size is None:
            missing.append("depth_at_size")

        sweep_buy = _sweep_vwap(snap.bids, snap.asks, Side.BUY, size)
        sweep_sell = _sweep_vwap(snap.bids, snap.asks, Side.SELL, size)
        eff_bid = sweep_sell
        eff_ask = sweep_buy
        if eff_bid is None:
            missing.append("effective_bid_at_size")
        if eff_ask is None:
            missing.append("effective_ask_at_size")
        if sweep_buy is None:
            missing.append("sweep_vwap_buy")
        if sweep_sell is None:
            missing.append("sweep_vwap_sell")

        return BasicFeatureSnapshot(
            snapshot_id=snap.snapshot_id,
            pair_snapshot_id=None,
            source=snap.source,
            book_age_ms=snap.book_age_ms,
            spread=snap.spread,
            mid=snap.mid,
            best_bid=snap.best_bid,
            best_ask=snap.best_ask,
            best_bid_size=snap.best_bid_size,
            best_ask_size=snap.best_ask_size,
            depth_at_size=depth_at_size,
            effective_bid_at_size=eff_bid,
            effective_ask_at_size=eff_ask,
            sweep_vwap_buy=sweep_buy,
            sweep_vwap_sell=sweep_sell,
            quality_verdict=quality_verdict,
            missing_fields=tuple(sorted(set(missing))),
        )

    @staticmethod
    def build_from_store(
        store: MarketStateStore,
        token_id,
        *,
        size: Decimal,
        quality_verdict: str | None = None,
        now=None,
    ) -> BasicFeatureSnapshot | None:
        snap = store.capture(token_id, now=now)
        if snap is None:
            return None
        return FeatureBuilder.build(snap, size=size, quality_verdict=quality_verdict)

    @staticmethod
    def build_pair(
        pair: PairMarketSnapshot,
        *,
        size: Decimal,
        quality_verdict: str | None = None,
    ) -> BasicFeatureSnapshot:
        yes_features = FeatureBuilder.build(pair.yes, size=size, quality_verdict=quality_verdict)
        no_features = FeatureBuilder.build(pair.no, size=size, quality_verdict=quality_verdict)
        missing = sorted(set(yes_features.missing_fields) | set(no_features.missing_fields))
        max_age = max(pair.yes.book_age_ms, pair.no.book_age_ms)
        return BasicFeatureSnapshot(
            snapshot_id=pair.yes.snapshot_id,
            pair_snapshot_id=pair.pair_snapshot_id,
            source=pair.yes.source,
            book_age_ms=max_age,
            spread=pair.yes.spread,
            mid=pair.yes.mid,
            best_bid=pair.yes.best_bid,
            best_ask=pair.yes.best_ask,
            best_bid_size=pair.yes.best_bid_size,
            best_ask_size=pair.yes.best_ask_size,
            depth_at_size=_min_optional(yes_features.depth_at_size, no_features.depth_at_size),
            effective_bid_at_size=_min_optional(
                yes_features.effective_bid_at_size, no_features.effective_bid_at_size
            ),
            effective_ask_at_size=_min_optional(
                yes_features.effective_ask_at_size, no_features.effective_ask_at_size
            ),
            sweep_vwap_buy=yes_features.sweep_vwap_buy,
            sweep_vwap_sell=yes_features.sweep_vwap_sell,
            quality_verdict=quality_verdict,
            missing_fields=tuple(missing),
        )


def _depth_at_size(snap: MarketStateSnapshot, size: Decimal) -> Decimal | None:
    if size <= 0:
        return None
    bid_depth = _walk_depth(snap.bids, size)
    ask_depth = _walk_depth(snap.asks, size)
    if bid_depth is None and ask_depth is None:
        return None
    if bid_depth is None:
        return ask_depth
    if ask_depth is None:
        return bid_depth
    return min(bid_depth, ask_depth)


def _walk_depth(levels, size: Decimal) -> Decimal | None:
    remaining = size
    filled = Decimal("0")
    for lv in levels:
        take = lv.size if lv.size < remaining else remaining
        filled += take
        remaining -= take
        if remaining <= 0:
            return filled
    return filled if filled > 0 else None


def _sweep_vwap(bids, asks, side: Side, size: Decimal) -> Decimal | None:
    if size <= 0:
        return None
    levels = asks if side == Side.BUY else bids
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


def _min_optional(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)
