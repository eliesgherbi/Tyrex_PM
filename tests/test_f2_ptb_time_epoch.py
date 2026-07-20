"""F2: PTB/K, TimeAuthority, atomic decision epoch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, MarketId
from tyrex_pm.core.time_authority import FakeTimeAuthority, TimeSyncStatus
from tyrex_pm.domain.polymarket.ptb import (
    PtbLockStore,
    PtbQuality,
    make_fixture_ptb,
)
from tyrex_pm.domain.polymarket.resolution import BinaryResolutionRule, ComparisonRule
from tyrex_pm.strategies.z_gap.snapshots import DecisionEpoch, require_compatible_epoch
from tyrex_pm.strategies.z_gap.reasons import ZGapReason

TS = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
MID = MarketId("m-f2")


def test_resolution_rule_contract() -> None:
    rule = BinaryResolutionRule(
        market_id=MID,
        window_id="w1",
        event_start=TS,
        event_end=TS + timedelta(minutes=5),
        comparison=ComparisonRule.AT_OR_ABOVE_K,
        resolution_reference_id="settlement-ref-fixture",
    )
    assert rule.comparison is ComparisonRule.AT_OR_ABOVE_K


def test_ptb_quality_classes_and_fixture() -> None:
    snap = make_fixture_ptb(
        market_id=MID,
        window_id="w1",
        event_start=TS,
        event_end=TS + timedelta(minutes=5),
        k="100.5",
        receive_ts=TS,
        quality=PtbQuality.CONFIRMED_CANONICAL,
    )
    assert snap.usable_for_entry
    assert snap.source_class.value == "FIXTURE"
    missing = make_fixture_ptb(
        market_id=MID,
        window_id="w1",
        event_start=TS,
        event_end=TS + timedelta(minutes=5),
        k=None,
        receive_ts=TS,
    )
    assert missing.quality is PtbQuality.MISSING
    assert not missing.usable_for_entry


def test_ptb_lock_immutability_and_mismatch() -> None:
    store = PtbLockStore()
    first = make_fixture_ptb(
        market_id=MID,
        window_id="w1",
        event_start=TS,
        event_end=TS + timedelta(minutes=5),
        k="100",
        receive_ts=TS,
    )
    locked = store.lock(first)
    assert locked.locked and locked.k == Decimal("100")
    # Second lock returns original immutable snapshot
    again = store.lock(
        make_fixture_ptb(
            market_id=MID,
            window_id="w1",
            event_start=TS,
            event_end=TS + timedelta(minutes=5),
            k="999",
            receive_ts=TS + timedelta(seconds=1),
        )
    )
    assert again.k == Decimal("100")
    mismatch = store.observe(
        make_fixture_ptb(
            market_id=MID,
            window_id="w1",
            event_start=TS,
            event_end=TS + timedelta(minutes=5),
            k="101",
            receive_ts=TS + timedelta(seconds=2),
        )
    )
    assert mismatch.quality is PtbQuality.MISMATCHED
    assert mismatch.mismatch
    # Locked store entry unchanged
    assert store.get_locked(MID, "w1").k == Decimal("100")


def test_fake_time_authority_deterministic() -> None:
    clock = FakeClock(_wall=TS)
    auth = FakeTimeAuthority(clock=clock, uncertainty_ms=0)
    v1 = auth.view()
    assert v1.ready and v1.corrected_utc == TS
    auth.advance(wall=timedelta(seconds=2), mono_ns=2_000_000_000)
    v2 = auth.view()
    assert v2.corrected_utc == TS + timedelta(seconds=2)
    assert v2.monotonic_ns == 2_000_000_000
    # Wall and mono are independent
    auth.advance(wall=timedelta(seconds=5), mono_ns=0)
    assert auth.monotonic_ns() == 2_000_000_000


def test_time_uncertainty_and_unsync() -> None:
    clock = FakeClock(_wall=TS)
    auth = FakeTimeAuthority(clock=clock, uncertainty_ms=500, max_uncertainty_ms=250)
    assert not auth.view().ready
    assert auth.view().reason_code == "time_uncertainty_exceeded"
    auth.set_uncertainty_ms(0)
    auth.set_sync_status(TimeSyncStatus.UNSYNCHRONIZED)
    assert not auth.view().ready
    assert auth.view().reason_code == "time_unsynchronized"


def test_epoch_match_and_mismatch() -> None:
    e1 = DecisionEpoch.new(
        market_id=MID,
        window_id="w1",
        evaluated_at=TS,
        correlation_id=CorrelationId("c1"),
    )
    e2 = DecisionEpoch.new(
        market_id=MID,
        window_id="w1",
        evaluated_at=TS,
        correlation_id=CorrelationId("c1"),
    )
    assert require_compatible_epoch(e1, other=e1) is None
    assert require_compatible_epoch(e1, other=e2) is ZGapReason.EPOCH_MISMATCH
    assert (
        require_compatible_epoch(e1, window_id="other") is ZGapReason.WINDOW_MISMATCH
    )
    assert (
        require_compatible_epoch(e1, market_id=MarketId("other"))
        is ZGapReason.WINDOW_MISMATCH
    )
