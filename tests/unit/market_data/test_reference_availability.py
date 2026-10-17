"""reference_availability_v1: bounded opening-price recovery from RTDS history.

Scenario from live run 5167e639: the TWAP stream went quiet just before 08:45:00 and
reconnected at 08:45:18; the 08:45:00 boundary tick was never pushed live, but RTDS
sends recent history on (re)subscription.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tyrex_pm.adapters.polymarket.rtds_normalize import (
    TOPIC_TWAP_SIXTY,
    normalize_chainlink_tick,
    normalize_twap_history,
)
from tyrex_pm.core.ids import MarketId
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryRuleId
from tyrex_pm.domain.polymarket.ptb_capture import PtbCaptureEngine
from tyrex_pm.domain.polymarket.reference_availability import (
    DELIVERY_HISTORY,
    POLICY_V1,
    ReferenceAvailabilityPolicy,
    is_history,
    selected_boundary_tick,
)
from tyrex_pm.runtime.market_event_router import _boundary_tick, route_boundary_history

START = datetime(2026, 10, 17, 8, 45, 0, tzinfo=timezone.utc)
MID = MarketId("m")
WID = "btc-updown-5m-1792226700"
V1 = ReferenceAvailabilityPolicy()


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def live_tick(value: str, at: datetime, *, received: datetime, generation: int = 1):
    msg = {
        "topic": TOPIC_TWAP_SIXTY,
        "type": "update",
        "timestamp": ms(received),
        "payload": {
            "symbol": "btc/usd",
            "timestamp": ms(at),
            "value": float(value),
            "full_accuracy_value": value,
        },
    }
    return normalize_chainlink_tick(
        msg,
        ts_received=received,
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=generation,
        clock_uncertainty_ms=None,
    )


def history(points, *, received: datetime, generation: int = 2):
    seq = iter(range(1, 10_000))
    data = [
        {"timestamp": ms(t), "value": float(v), "full_accuracy_value": v} for t, v in points
    ]
    msg = {
        "topic": TOPIC_TWAP_SIXTY,
        "type": "subscribe",
        "timestamp": ms(received),
        "payload": {"symbol": "btc/usd", "data": data},
    }
    return normalize_twap_history(
        msg,
        ts_received=received,
        receive_monotonic_ns=2,
        next_sequence=lambda: next(seq),
        connection_generation=generation,
        clock_uncertainty_ms=None,
    )


class Runtime(SimpleNamespace):
    """Just enough of MarketSessionRuntime: history may only reach the PTB engine."""

    def ingest_boundary_history(self, *, window_id, market_id, tick):
        self.ptb_engine.ingest_chainlink(market_id=market_id, window_id=window_id, tick=tick)

    def ingest_chainlink(self, **_kwargs):  # the live path (latest price, basis)
        raise AssertionError("history must never update the latest reference")


def runtime() -> Runtime:
    engine = PtbCaptureEngine()
    engine.open_window(
        market_id=MID, window_id=WID, event_start=START, event_end=START + timedelta(minutes=5)
    )
    sess = SimpleNamespace(market_id=MID, window_id=WID, event_start=START)
    return Runtime(active=sess, prepared_next=None, ptb_engine=engine)


def test_policy_exact_timestamp_and_bounded_deadline() -> None:
    usable = V1.history_usable_for_boundary
    after_18s = START + timedelta(seconds=18)
    assert usable(source_ts=START, received_at=after_18s, window_start=START,
                  already_sealed=False)
    # No substitution with a nearby timestamp.
    assert not usable(source_ts=START + timedelta(seconds=1), received_at=after_18s,
                      window_start=START, already_sealed=False)
    assert not usable(source_ts=START, received_at=START + timedelta(seconds=31),
                      window_start=START, already_sealed=False)
    # Immutability: a sealed opening price is never revisited.
    assert not usable(source_ts=START, received_at=after_18s, window_start=START,
                      already_sealed=True)
    assert V1.arrival_ok(lag_ms=4_000, recovered_from_history=False)
    assert not V1.arrival_ok(lag_ms=18_000, recovered_from_history=False)  # 5 s stays
    assert V1.arrival_ok(lag_ms=18_000, recovered_from_history=True)
    assert not V1.arrival_ok(lag_ms=31_000, recovered_from_history=True)
    legacy = ReferenceAvailabilityPolicy.legacy()
    assert legacy.arrival_ok(lag_ms=None, recovered_from_history=False)
    assert not legacy.history_usable_for_boundary(
        source_ts=START, received_at=START, window_start=START, already_sealed=False
    )
    with pytest.raises(ValueError):
        ReferenceAvailabilityPolicy(policy_id="v0")


def test_history_ticks_keep_source_time_receipt_time_and_generation() -> None:
    received = START + timedelta(seconds=18)
    events = history(
        [(START - timedelta(seconds=1), "81579.1"), (START, "81579.37638532958")],
        received=received,
        generation=2,
    )
    assert [e.ts_event for e in events] == [START - timedelta(seconds=1), START]
    assert all(e.ts_received == received for e in events)  # availability = arrival
    assert all(is_history(e.ingress) and e.ingress.connection_generation == 2 for e in events)
    assert events[1].ingress.delivery_kind == DELIVERY_HISTORY
    assert events[1].settlement.price == Decimal("81579.37638532958")


def test_reconnect_gap_recovers_the_exact_boundary_from_history() -> None:
    rt = runtime()
    before = live_tick(
        "81570.0", START - timedelta(seconds=2), received=START - timedelta(seconds=1)
    )
    rt.ptb_engine.ingest_chainlink(market_id=MID, window_id=WID, tick=_boundary_tick(before))
    assert rt.ptb_engine.get_window(MID, WID).selected_candidate is None
    received = START + timedelta(seconds=18)
    snapshot = history(
        [
            (START - timedelta(seconds=1), "81571.0"),
            (START, "81579.376"),
            (START + timedelta(seconds=1), "81579.9"),
        ],
        received=received,
    )
    routed = [w for e in snapshot for w in route_boundary_history(rt, e, V1)]
    assert routed == [WID]  # only the exact boundary tick is offered
    state = rt.ptb_engine.get_window(MID, WID)
    assert state.selected_candidate.rule_id is BoundaryRuleId.EXACT_AT_START
    assert state.selected_candidate.value == Decimal("81579.376")
    tick = selected_boundary_tick(state)
    assert is_history(tick.ingress) and tick.receive_wall_raw_utc == received
    sealed = rt.ptb_engine.seal(market_id=MID, window_id=WID, sealed_at=received)
    assert sealed.ptb_k == Decimal("81579.376") and sealed.boundary_lag_ms == 18_000
    assert V1.arrival_ok(lag_ms=sealed.boundary_lag_ms, recovered_from_history=True)
    # A later history copy can never replace an established opening price.
    later = history([(START, "81000")], received=received + timedelta(seconds=2))
    assert route_boundary_history(rt, later[0], V1) == ()


def test_history_past_the_deadline_is_not_used() -> None:
    rt = runtime()
    late = history([(START, "81579.376")], received=START + timedelta(seconds=40))
    assert route_boundary_history(rt, late[0], V1) == ()
    assert rt.ptb_engine.get_window(MID, WID).selected_candidate is None


def test_identical_duplicate_is_harmless_and_a_conflict_blocks() -> None:
    rt = runtime()
    live = live_tick("81579.376", START, received=START + timedelta(seconds=1))
    rt.ptb_engine.ingest_chainlink(market_id=MID, window_id=WID, tick=_boundary_tick(live))
    same = history([(START, "81579.376")], received=START + timedelta(seconds=18))
    route_boundary_history(rt, same[0], V1)
    assert "conflicting_duplicate" not in rt.ptb_engine.get_window(MID, WID).blockers
    other = history([(START, "81580.0")], received=START + timedelta(seconds=19))
    route_boundary_history(rt, other[0], V1)
    assert "conflicting_duplicate" in rt.ptb_engine.get_window(MID, WID).blockers


def test_replay_policy_selects_the_same_rule() -> None:
    from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy

    assert ReplayExecutionPolicy().reference_policy().is_legacy  # reproduces research
    v1 = ReplayExecutionPolicy(reference_availability=POLICY_V1)
    assert v1.reference_policy() == V1
    assert v1.identity()["reference_availability"] == POLICY_V1
    with pytest.raises(ValueError):
        ReplayExecutionPolicy(reference_availability="v0")
