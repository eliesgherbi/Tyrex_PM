from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from tyrex_pm.adapters.clock_sync import OsMonitorClockSyncProvider
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.time_authority import (
    ClockSyncSnapshot,
    SnapshotTimeAuthority,
    TimeSyncStatus,
)


@pytest.mark.asyncio
async def test_clock_provider_keeps_lowest_latency_fresh_wifi_sample() -> None:
    rtts = iter((620.0, 40.0))

    def fetch_time() -> tuple[float, float]:
        return time.time() * 1_000.0, next(rtts)

    provider = OsMonitorClockSyncProvider(time_fetcher=fetch_time)
    first = await provider.measure()
    second = await provider.measure()
    assert first.uncertainty_ms == 311
    assert second.uncertainty_ms == 21
    assert second.primary_source == "binance_api_time_best_of_window"
    assert any(source.detail == "best_of_2_fresh_samples" for source in second.sources)


def test_snapshot_age_uses_monotonic_not_adjustable_wall_time() -> None:
    wall = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(wall, _mono_ns=1_000_000_000)
    authority = SnapshotTimeAuthority(
        clock=clock,
        max_uncertainty_ms=100,
        max_snapshot_age_ms=5_000,
    )
    authority.apply_snapshot(
        ClockSyncSnapshot(
            measured_at_wall_utc=wall,
            measured_at_monotonic_ns=1_000_000_000,
            estimated_offset_ms=-3_000,
            uncertainty_ms=20,
            sync_status=TimeSyncStatus.READY,
            primary_source="test",
        )
    )
    clock.advance(wall=timedelta(seconds=30), mono_ns=1_000_000_000)
    view = authority.view()
    assert view.snapshot_age_ms == 1_000
    assert view.ready is True
