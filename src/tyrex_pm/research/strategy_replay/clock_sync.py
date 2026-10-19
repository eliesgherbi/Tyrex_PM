"""Recorded clock observations → the replay's corrected time (as live clock sync does).

The collector host clock is not the venue's clock: on 6 October 2026 it ran about
330–570 ms behind Binance, so raw receive times make most Binance prints look like they
arrived before they happened, and the host's causal pairing rejects them as future
prints. Live, ``OsMonitorClockSyncProvider`` corrects time against Binance server time.
Replay does the same with the warehouse's recorded ``clock_observations``; each
observation applies from the moment it was received, and before the first accepted one
the clock is unsynchronized and evaluations are skipped.

Policy (``CLOCK_POLICY``), mirroring live:

* only ``binance_time`` samples correct the clock. The collector also records
  ``clob_time``, whose server time has whole-second resolution: its offset is off by up
  to a second while its recorded uncertainty was only half the round trip. Mixing it in
  made the corrected clock jump by ~0.6 s between sources and turned most Binance prints
  into "future" prints (60–80% of trades in the S2 development windows);
* the offset comes from the lowest-round-trip sample among the last six accepted
  samples received within 90 s of the newest; uncertainty is half that round trip + 1 ms.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tyrex_pm.core.time_authority import (
    FakeTimeAuthority,
    TimeAuthorityView,
    TimeSyncStatus,
    _readiness,
)

CLOCK_POLICY = "binance_time_best_rtt_of_6_within_90s_v1"
ACCEPTED_SOURCES = frozenset({"binance_time"})
# Captured runs (capture_replay): the live ``ClockSyncSnapshot`` applied at its live
# apply time, interpreted as ``SnapshotTimeAuthority`` does (same age rule).
LIVE_SNAPSHOT_SOURCE = "tyrex_live_clock_snapshot"
LIVE_SNAPSHOT_POLICY = "captured_live_clock_snapshots_v1"
LIVE_SNAPSHOT_MAX_AGE_MS = 60_000
SAMPLE_WINDOW = 6
SAMPLE_MAX_AGE_NS = 90 * 1_000_000_000


@dataclass
class RecordedClockAuthority(FakeTimeAuthority):
    sync_status: TimeSyncStatus = TimeSyncStatus.UNSYNCHRONIZED
    observations_applied: int = 0
    last_sample_id: str | None = None
    rejected_sources: dict[str, int] = field(default_factory=dict)
    _samples: list[dict] = field(default_factory=list, repr=False)
    _live: dict | None = field(default=None, repr=False)

    def apply(self, row: dict) -> None:
        source = str(row.get("measurement_source") or "")
        if source.startswith(LIVE_SNAPSHOT_SOURCE + ":"):
            # Warehouse encoding of a captured live snapshot (capture_replay): status in
            # the source name, measurement wall time in server_at, offset/uncertainty ns.
            measured = row.get("server_at")
            measured_ns = (int(measured.timestamp() * 1e9) if isinstance(measured, datetime)
                           else int(measured))
            self._live = {"measured_at_ns": measured_ns}
            self.estimated_offset_ms = float(row["estimated_offset_ns"]) / 1e6
            self.uncertainty_ms = int(math.ceil(float(row["uncertainty_ns"]) / 1e6))
            self.sync_status = TimeSyncStatus(source.split(":", 1)[1])
            self.observations_applied += 1
            self.last_sample_id = row.get("sample_id")
            return
        if row.get("status") != "OBSERVED" or row.get("estimated_offset_ns") is None:
            return
        source = row.get("measurement_source")
        if source not in ACCEPTED_SOURCES:
            key = str(source)
            self.rejected_sources[key] = self.rejected_sources.get(key, 0) + 1
            return
        rtt = row.get("rtt_ns")
        rtt_ns = float(rtt) if rtt is not None else 2 * float(row.get("uncertainty_ns") or 0)
        received = row.get("received_at")
        self._samples.append(
            {
                "offset_ns": float(row["estimated_offset_ns"]),
                "rtt_ns": max(0.0, rtt_ns),
                "received_at": received,
                "sample_id": row.get("sample_id"),
            }
        )
        recent = self._samples[-SAMPLE_WINDOW:]
        if received is not None:
            oldest = received - SAMPLE_MAX_AGE_NS
            recent = [s for s in recent if s["received_at"] is None or s["received_at"] >= oldest]
        self._samples = recent
        # Lowest delay = narrowest interval around the server time (as live).
        best = min(self._samples, key=lambda s: s["rtt_ns"])
        self.estimated_offset_ms = best["offset_ns"] / 1e6
        self.uncertainty_ms = int(math.ceil(best["rtt_ns"] / 2 / 1e6)) + 1
        self.sync_status = TimeSyncStatus.READY
        self.observations_applied += 1
        self.last_sample_id = best["sample_id"]

    @property
    def policy(self) -> str:
        """The correction actually applied (captured live snapshots or Binance samples)."""
        return LIVE_SNAPSHOT_POLICY if self._live is not None else CLOCK_POLICY

    @property
    def offset(self) -> timedelta:
        return timedelta(milliseconds=self.estimated_offset_ms)

    def view(self) -> TimeAuthorityView:
        if self._live is not None:
            return self._live_view()
        ready, reason = _readiness(
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            max_uncertainty_ms=self.max_uncertainty_ms,
        )
        return TimeAuthorityView(
            corrected_utc=self.clock.now_utc() + self.offset,
            monotonic_ns=self.clock.monotonic_ns(),
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            ready=ready,
            reason_code=reason,
            estimated_offset_ms=self.estimated_offset_ms,
            clock_snapshot_id=self.last_sample_id,
        )

    def _live_view(self) -> TimeAuthorityView:
        snap = self._live
        now = self.clock.now_utc()
        measured = datetime.fromtimestamp(snap["measured_at_ns"] / 1e9, tz=now.tzinfo)
        age_ms = max(0, int((now - measured).total_seconds() * 1000))
        status, uncertainty = self.sync_status, self.uncertainty_ms
        if age_ms > LIVE_SNAPSHOT_MAX_AGE_MS:
            status = TimeSyncStatus.DEGRADED
            uncertainty = max(uncertainty, age_ms)
        ready, reason = _readiness(sync_status=status, uncertainty_ms=uncertainty,
                                   max_uncertainty_ms=self.max_uncertainty_ms)
        return TimeAuthorityView(
            corrected_utc=now + self.offset,
            monotonic_ns=self.clock.monotonic_ns(),
            sync_status=status,
            uncertainty_ms=uncertainty,
            ready=ready,
            reason_code=reason,
            estimated_offset_ms=self.estimated_offset_ms,
            snapshot_age_ms=age_ms,
            clock_snapshot_id=self.last_sample_id,
        )

    def corrected(self, raw: datetime) -> datetime:
        return raw + self.offset
