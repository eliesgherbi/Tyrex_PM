"""Clock synchronization providers (N2) — network I/O outside core.

Emits ``ClockSyncSnapshot`` for ``SnapshotTimeAuthority`` to interpret.
Never mutates the operating-system clock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from urllib.request import Request, urlopen

from tyrex_pm.core.time_authority import (
    ClockSourceObservation,
    ClockSyncSnapshot,
    TimeSyncStatus,
)

logger = logging.getLogger(__name__)

BINANCE_TIME_URL = "https://api.binance.com/api/v3/time"


class ClockSyncProvider(Protocol):
    async def measure(self) -> ClockSyncSnapshot: ...


def _wall_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FakeClockSyncProvider:
    """Deterministic provider for offline tests."""

    offset_ms: float = 0.0
    uncertainty_ms: int = 5
    sync_status: TimeSyncStatus = TimeSyncStatus.READY
    primary_source: str = "fake"
    sources: tuple[ClockSourceObservation, ...] = ()
    disagreement_ms: float | None = None
    _mono: int = 0

    async def measure(self) -> ClockSyncSnapshot:
        self._mono += 1_000_000
        src = self.sources or (
            ClockSourceObservation(
                source=self.primary_source,
                offset_ms=self.offset_ms,
                round_trip_ms=1.0,
                uncertainty_ms=float(self.uncertainty_ms),
                ok=True,
            ),
        )
        return ClockSyncSnapshot(
            measured_at_wall_utc=_wall_now(),
            measured_at_monotonic_ns=self._mono,
            estimated_offset_ms=self.offset_ms,
            uncertainty_ms=self.uncertainty_ms,
            sync_status=self.sync_status,
            primary_source=self.primary_source,
            sources=tuple(src),
            max_source_disagreement_ms=self.disagreement_ms,
            valid_for_ms=60_000,
        )


@dataclass
class OsMonitorClockSyncProvider:
    """Monitor OS clock; optional Binance ``/api/v3/time`` cross-check.

    Does not call SNTP by default (platform-dependent). Cross-check is
    injectable for tests via ``time_fetcher``.
    """

    binance_time_url: str = BINANCE_TIME_URL
    enable_binance_cross_check: bool = True
    disagreement_degraded_ms: float = 500.0
    opener: Any = urlopen
    time_fetcher: Callable[[], tuple[float, float]] | None = None
    # time_fetcher returns (remote_utc_ms, rtt_ms)

    def _fetch_binance_time(self) -> tuple[float, float]:
        if self.time_fetcher is not None:
            return self.time_fetcher()
        t0 = time.perf_counter()
        req = Request(
            self.binance_time_url,
            headers={"User-Agent": "tyrex-pm/0.3 (clock-sync; read-only)"},
        )
        with self.opener(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        t1 = time.perf_counter()
        remote_ms = float(payload["serverTime"])
        rtt_ms = (t1 - t0) * 1000.0
        return remote_ms, rtt_ms

    async def measure(self) -> ClockSyncSnapshot:
        wall = _wall_now()
        mono = time.perf_counter_ns()
        local_ms = wall.timestamp() * 1000.0
        sources: list[ClockSourceObservation] = [
            ClockSourceObservation(
                source="os_clock",
                offset_ms=0.0,
                round_trip_ms=0.0,
                uncertainty_ms=None,
                ok=True,
                detail="application monitors OS clock; does not set it",
            )
        ]
        status = TimeSyncStatus.READY
        uncertainty = 0
        primary = "os_clock"

        if self.enable_binance_cross_check:
            try:
                remote_ms, rtt_ms = await asyncio.to_thread(self._fetch_binance_time)
                # Correct for one-way delay ≈ RTT/2
                offset = (remote_ms + rtt_ms / 2.0) - local_ms
                sources.append(
                    ClockSourceObservation(
                        source="binance_api_time",
                        offset_ms=offset,
                        round_trip_ms=rtt_ms,
                        uncertainty_ms=rtt_ms / 2.0,
                        ok=True,
                    )
                )
                uncertainty = max(uncertainty, int(rtt_ms / 2.0) + 1)
            except Exception as exc:
                logger.info("binance time cross-check failed: %s", exc)
                sources.append(
                    ClockSourceObservation(
                        source="binance_api_time",
                        offset_ms=None,
                        ok=False,
                        detail=f"{type(exc).__name__}:{exc}",
                    )
                )
                status = TimeSyncStatus.DEGRADED
                uncertainty = max(uncertainty, 250)

        # OS row is a monitor baseline (offset_ms=0 by definition), not a second
        # independent estimate of "true" time. A non-zero Binance cross-check is
        # the estimated_offset to *apply*, not a disagreement that forces DEGRADED.
        # DEGRADED only when the cross-check fails (above) or when multiple
        # successful *external* sources diverge beyond the threshold.
        external_ok = [
            s
            for s in sources
            if s.source != "os_clock" and s.ok and s.offset_ms is not None
        ]
        external_offsets = [float(s.offset_ms) for s in external_ok]  # type: ignore[arg-type]
        disagreement = (
            max(external_offsets) - min(external_offsets)
            if len(external_offsets) > 1
            else 0.0
        )
        est_offset = 0.0
        binance_obs = next((s for s in sources if s.source == "binance_api_time" and s.ok), None)
        if binance_obs is not None and binance_obs.offset_ms is not None:
            est_offset = float(binance_obs.offset_ms)
        if (
            len(external_offsets) > 1
            and abs(disagreement) >= self.disagreement_degraded_ms
        ):
            status = TimeSyncStatus.DEGRADED
            uncertainty = max(uncertainty, int(abs(disagreement)))

        return ClockSyncSnapshot(
            measured_at_wall_utc=wall,
            measured_at_monotonic_ns=mono,
            estimated_offset_ms=est_offset,
            uncertainty_ms=uncertainty,
            sync_status=status,
            primary_source=primary,
            sources=tuple(sources),
            max_source_disagreement_ms=(
                disagreement if len(external_offsets) > 1 else None
            ),
            valid_for_ms=60_000,
        )


@dataclass
class ClockSyncLoop:
    """Periodically measure and apply snapshots to a SnapshotTimeAuthority."""

    provider: ClockSyncProvider
    apply: Callable[[ClockSyncSnapshot], None]
    interval_s: float = 30.0
    on_snapshot: Callable[[ClockSyncSnapshot], None] | None = None

    def __post_init__(self) -> None:
        self._stop = asyncio.Event()
        self.last_snapshot: ClockSyncSnapshot | None = None

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                snap = await self.provider.measure()
                self.last_snapshot = snap
                self.apply(snap)
                if self.on_snapshot:
                    self.on_snapshot(snap)
            except Exception as exc:
                logger.warning("clock sync measure failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue
