"""Virtual time in freshness paths, the shared router, and proxy outcome labels."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from tyrex_pm.core.book_events import BookSnapshotReceived
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.events import EventSource, ReferencePriceUpdated
from tyrex_pm.core.ids import CorrelationId, EventId, InstrumentId
from tyrex_pm.core.snapshots import BookLevel, BookSnapshot, ReferencePriceSnapshot
from tyrex_pm.facts import ids as F
from tyrex_pm.indicators.graph import IndicatorGraph
from tyrex_pm.market_data.book_health import SyncHealth
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.research.strategy_replay.labels import engine_labels, proxy_outcome
from tyrex_pm.runtime.market_event_router import route_trading_reference
from tyrex_pm.strategies.settlement_value.config import SettlementVolatilityConfig
from tyrex_pm.strategies.settlement_value.plugin import ewma_vol_spec

T0 = datetime(2026, 10, 6, 17, 50, tzinfo=timezone.utc)
NS = 1_000_000_000


def clock_at(seconds: float) -> FakeClock:
    clock = FakeClock(_wall=T0)
    clock.advance_to_epoch_ns(int(T0.timestamp() * NS + seconds * NS))
    return clock


def test_graph_staleness_follows_virtual_time_not_host_time() -> None:
    clock = clock_at(0)
    graph = IndicatorGraph(
        (ewma_vol_spec(SettlementVolatilityConfig(min_samples_s=2.0)),),
        max_trade_age_ms=2000,
        clock=clock,
    )
    base = int(T0.timestamp() * NS)
    for i in range(30):
        clock.advance_to_epoch_ns(base + i * NS)
        graph.on_trade(
            source=F.BINANCE_SPOT_TRADES,
            price=Decimal("100000") + Decimal(i % 3),
            ts_event=datetime.fromtimestamp(T0.timestamp() + i, tz=timezone.utc),
        )
    (snap,) = graph.snapshot().values.values()
    assert snap.ready
    # A recorded 5 s silence is stale under virtual time, however fast the replay runs.
    clock.advance_to_epoch_ns(base + 34 * NS)
    (stale,) = graph.snapshot().values.values()
    assert not stale.ready and stale.reject_reason == "stale_tape"


def test_book_store_staleness_follows_injected_clock() -> None:
    clock = clock_at(0)
    store = MarketStateStore(clock=clock)
    iid = InstrumentId("up")
    book = BookSnapshot(
        instrument_id=iid, ts_event=T0,
        bids=(BookLevel(price=Decimal("0.4"), quantity=Decimal("1")),),
        asks=(BookLevel(price=Decimal("0.5"), quantity=Decimal("1")),),
    )
    store.on_snapshot(
        BookSnapshotReceived(
            event_id=EventId("e"), correlation_id=CorrelationId("c"), ts_event=T0,
            ts_received=T0, source=EventSource.POLYMARKET_CLOB, book=book,
        )
    )
    assert store.get(iid).sync_health is SyncHealth.READY
    clock.advance_to_epoch_ns(int(T0.timestamp() * NS) + 31 * NS)
    store.mark_stale_if_aged()
    assert store.get(iid).sync_health is SyncHealth.STALE


def test_router_updates_indicators_exactly_once_per_trade() -> None:
    calls: list[Decimal] = []

    class Runtime:
        def ingest_binance(self, tick):
            calls.append(tick.value)

    event = ReferencePriceUpdated(
        event_id=EventId("e"), correlation_id=CorrelationId("c"), ts_event=T0,
        ts_received=T0, source=EventSource.BINANCE,
        reference=ReferencePriceSnapshot(symbol="BTCUSDT", price=Decimal("1"), ts_event=T0),
    )
    assert route_trading_reference(Runtime(), event) is True
    assert calls == [Decimal("1")]
    other = ReferencePriceUpdated(
        event_id=EventId("e2"), correlation_id=CorrelationId("c"), ts_event=T0,
        ts_received=T0, source=EventSource.POLYMARKET_RTDS_BINANCE,
        reference=ReferencePriceSnapshot(symbol="BTCUSDT", price=Decimal("2"), ts_event=T0),
    )
    assert route_trading_reference(Runtime(), other) is False
    assert calls == [Decimal("1")]


def window(rows):
    return SimpleNamespace(
        resolution_rows=rows,
        up_outcome_index=0,
        down_outcome_index=1,
        up_instrument_id="up",
        down_instrument_id="down",
    )


def resolved(payouts, at=10):
    return {
        "provider_status": "resolved",
        "payouts": [Decimal(p) for p in payouts],
        "payout_unit": "UNKNOWN",
        "received_at": at,
        "resolved_at": 5,
        "transaction_hash": "0x",
    }


def test_proxy_outcome_is_explicitly_labelled_and_conservative() -> None:
    label = proxy_outcome(window([resolved(["1000000", "0"])]))
    assert label.label_type == "proxy_outcome" and label.winner == "UP"
    assert label.payout_unit == "UNKNOWN"
    labels = engine_labels(window([]), label)
    assert {(x["instrument_id"], x["payout"], x["status"]) for x in labels} == {
        ("up", "1", "PROXY_OUTCOME"),
        ("down", "0", "PROXY_OUTCOME"),
    }
    assert proxy_outcome(window([])).winner is None
    split = proxy_outcome(window([resolved(["500000", "500000"])]))
    assert split.winner is None and split.reason == "payout_vector_not_single_winner"
    conflict = proxy_outcome(window([resolved(["1", "0"]), resolved(["0", "1"], at=11)]))
    assert conflict.winner is None and conflict.reason == "conflicting_payout_vectors"


def test_recorded_clock_authority_is_unsynchronized_until_an_observation() -> None:
    from datetime import timedelta

    from tyrex_pm.research.strategy_replay.clock_sync import RecordedClockAuthority

    clock = clock_at(10)
    authority = RecordedClockAuthority(clock=clock, max_uncertainty_ms=250)
    assert not authority.view().ready
    authority.apply({"status": "FAILED", "estimated_offset_ns": 1,
                     "measurement_source": "binance_time"})
    assert not authority.view().ready
    authority.apply({"status": "OBSERVED", "estimated_offset_ns": 300_000_000,
                     "rtt_ns": 160_000_000, "measurement_source": "binance_time",
                     "received_at": 1, "sample_id": "s1"})
    view = authority.view()
    assert view.ready and view.uncertainty_ms == 81 and view.clock_snapshot_id == "s1"
    assert view.corrected_utc - clock.now_utc() == timedelta(milliseconds=300)


def binance(offset_ms: float, rtt_ms: float, at_s: float, sample: str) -> dict:
    return {"status": "OBSERVED", "measurement_source": "binance_time", "sample_id": sample,
            "estimated_offset_ns": int(offset_ms * 1e6), "rtt_ns": int(rtt_ms * 1e6),
            "received_at": int(at_s * NS)}


def test_whole_second_clob_time_never_corrects_the_replay_clock() -> None:
    from tyrex_pm.research.strategy_replay.clock_sync import RecordedClockAuthority

    authority = RecordedClockAuthority(clock=clock_at(10), max_uncertainty_ms=250)
    authority.apply({"status": "OBSERVED", "measurement_source": "clob_time",
                     "estimated_offset_ns": -665_000_000, "rtt_ns": 50_000_000,
                     "received_at": 0})
    assert not authority.view().ready  # a coarse source alone does not synchronize
    authority.apply(binance(213, 300, 1, "b1"))
    authority.apply({"status": "OBSERVED", "measurement_source": "clob_time",
                     "estimated_offset_ns": -665_000_000, "rtt_ns": 20_000_000,
                     "received_at": 2 * NS})
    assert authority.estimated_offset_ms == 213  # unchanged by the later clob sample
    assert authority.rejected_sources == {"clob_time": 2}


def test_offset_follows_lowest_round_trip_of_recent_samples_like_live() -> None:
    from tyrex_pm.research.strategy_replay.clock_sync import RecordedClockAuthority

    authority = RecordedClockAuthority(clock=clock_at(10), max_uncertainty_ms=250)
    authority.apply(binance(200, 100, 0, "fast"))
    authority.apply(binance(260, 600, 30, "slow"))  # noisy sample does not win
    assert authority.estimated_offset_ms == 200 and authority.uncertainty_ms == 51
    assert authority.view().ready
    authority.apply(binance(250, 400, 100, "late"))  # "fast" is now older than 90 s
    assert authority.estimated_offset_ms == 250 and authority.last_sample_id == "late"
    authority.apply(binance(0, 800, 101, "worse"))  # uncertainty 401 ms alone, but
    assert authority.last_sample_id == "late"  # the 400 ms sample is still fresh
    for i in range(6):  # six newer slow samples push every earlier one out
        authority.apply(binance(0, 800, 102 + i, f"n{i}"))
    assert authority.uncertainty_ms == 401 and not authority.view().ready
