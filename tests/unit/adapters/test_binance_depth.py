"""G7 Binance spot L2 fixture → DepthStore mid. Perp stays unimplemented."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.adapters.binance.depth_adapter import depth_snapshot_from_binance
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.facts import ids as F
from tyrex_pm.facts.mappings import ASK70_INPUT_CONTRACT, Q_EDGE_INPUT_CONTRACT
from tyrex_pm.indicators.depth import DepthStore
from tyrex_pm.runtime.composition import missing_live_adapters, starts_adapter


def test_spot_l2_fixture_writes_depth_and_mid() -> None:
    store = DepthStore()
    snapshot = depth_snapshot_from_binance(
        {
            "lastUpdateId": 1,
            "bids": [["100.0", "2"], ["99.5", "1"]],
            "asks": [["100.2", "3"], ["100.4", "4"]],
        },
        symbol="btcusdt",
        ts_event=datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc),
    )
    store.apply(snapshot)
    held = store.capture(InstrumentId("BTCUSDT"))
    assert held is not None
    assert held.best_bid is not None
    assert held.best_ask is not None
    assert held.best_bid.price == Decimal("100.0")
    assert held.best_ask.price == Decimal("100.2")
    assert held.mid == Decimal("100.1")


def test_ask70_still_does_not_start_binance() -> None:
    assert not starts_adapter(ASK70_INPUT_CONTRACT, F.BINANCE_SPOT_L2)
    assert not starts_adapter(ASK70_INPUT_CONTRACT, F.BINANCE_SPOT_TRADES)


def test_q_edge_perp_facts_remain_unimplemented() -> None:
    missing = missing_live_adapters(Q_EDGE_INPUT_CONTRACT)
    assert F.BINANCE_SPOT_L2 not in missing
    assert F.BINANCE_PERP_L2 in missing
    assert F.BINANCE_PERP_FUNDING in missing
