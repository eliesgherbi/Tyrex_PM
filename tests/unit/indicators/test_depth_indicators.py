"""Generic depth store and reusable indicator producers."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.facts import ids as F
from tyrex_pm.indicators.depth import DepthLevel, DepthSnapshot
from tyrex_pm.indicators.graph import IndicatorGraph
from tyrex_pm.indicators.imbalance import imbalance, microprice, microprice_gap_bps
from tyrex_pm.indicators.ofi import l1_ofi_delta
from tyrex_pm.indicators.spec import IndicatorSpec


def _book(*, bid: str, bid_qty: str, ask: str, ask_qty: str) -> DepthSnapshot:
    return DepthSnapshot(
        instrument_id=InstrumentId("btcusdt"),
        ts_event=datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
        bids=(DepthLevel(price=Decimal(bid), quantity=Decimal(bid_qty)),),
        asks=(DepthLevel(price=Decimal(ask), quantity=Decimal(ask_qty)),),
    )


def test_imbalance_and_microprice_on_generic_depth() -> None:
    snap = _book(bid="64000", bid_qty="5", ask="64010", ask_qty="1")
    imb = imbalance(snap, levels=1)
    assert imb is not None and imb > 0
    mp = microprice(snap)
    assert mp is not None
    assert mp > snap.mid
    gap = microprice_gap_bps(snap)
    assert gap is not None and gap > 0


def test_l1_ofi_bid_add_is_positive() -> None:
    prev = _book(bid="64000", bid_qty="1", ask="64010", ask_qty="1")
    cur = _book(bid="64000", bid_qty="4", ask="64010", ask_qty="1")
    assert l1_ofi_delta(prev, cur) == Decimal("3")


def test_indicator_graph_runs_only_subscribed_specs() -> None:
    spec = IndicatorSpec(name="imbalance", source=F.BINANCE_SPOT_L2, levels=(1,))
    graph = IndicatorGraph((spec,))
    snap = _book(bid="100", bid_qty="2", ask="101", ask_qty="2")
    bundle = graph.on_depth(snap, source=F.BINANCE_SPOT_L2)
    key = f"{spec.instance_id}|1"
    assert bundle.get(key) == Decimal("0")
    empty = graph.on_depth(snap, source=F.BINANCE_PERP_L2)
    assert empty.get(key) == Decimal("0")
