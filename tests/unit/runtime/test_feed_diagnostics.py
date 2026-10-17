"""BTC reference freshness watchdog (separate from socket liveness)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tyrex_pm.runtime.feed_diagnostics import ReferenceFreshnessWatchdog

T0 = datetime(2026, 10, 17, 8, 44, 58, tzinfo=timezone.utc)


def test_stall_is_reported_once_and_resumption_measures_the_gap() -> None:
    dog = ReferenceFreshnessWatchdog(stale_after_s=3.0)
    assert dog.check(100.0) == []  # nothing seen yet
    assert dog.on_tick(source_ts=T0, receive_mono_s=100.0) == []
    assert dog.check(102.5) == []
    (stale,) = dog.check(103.5)
    assert stale[0] == "REFERENCE_STALE" and stale[1]["last_source_ts"] == T0.isoformat()
    assert dog.check(110.0) == []  # one report per stall
    (fresh,) = dog.on_tick(source_ts=T0 + timedelta(seconds=20), receive_mono_s=118.0)
    assert fresh[0] == "REFERENCE_FRESH_AGAIN" and fresh[1]["gap_s"] == 18.0
    assert fresh[1]["first_source_ts_after_gap"] == (T0 + timedelta(seconds=20)).isoformat()
    assert dog.on_tick(source_ts=T0 + timedelta(seconds=21), receive_mono_s=119.0) == []
