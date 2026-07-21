"""Time-authority contract for model evaluation.

Core interprets ``ClockSyncSnapshot`` only — no SNTP/HTTP/WebSocket I/O here.
Network sync providers live under ``adapters/`` (N2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Protocol, runtime_checkable

from tyrex_pm.core.clock import Clock, FakeClock, require_utc


class TimeSyncStatus(str, Enum):
    READY = "READY"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, kw_only=True)
class ClockSourceObservation:
    """One cross-check from a clock source (OS / SNTP / Binance time, etc.)."""

    source: str
    offset_ms: float | None
    round_trip_ms: float | None = None
    uncertainty_ms: float | None = None
    ok: bool = True
    detail: str | None = None


@dataclass(frozen=True, kw_only=True)
class ClockSyncSnapshot:
    """Normalized sync evidence emitted by an adapter/ops provider.

    Core TimeAuthority consumes this; it never fetches network time itself.
    Application must not silently change the OS clock.
    """

    measured_at_wall_utc: datetime
    measured_at_monotonic_ns: int
    estimated_offset_ms: float
    uncertainty_ms: int
    sync_status: TimeSyncStatus
    primary_source: str
    sources: tuple[ClockSourceObservation, ...] = ()
    max_source_disagreement_ms: float | None = None
    valid_for_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "measured_at_wall_utc",
            require_utc(self.measured_at_wall_utc, field_name="measured_at_wall_utc"),
        )
        if self.uncertainty_ms < 0:
            raise ValueError("uncertainty_ms must be >= 0")
        if self.measured_at_monotonic_ns < 0:
            raise ValueError("measured_at_monotonic_ns must be >= 0")


@dataclass(frozen=True, kw_only=True)
class TimeAuthorityView:
    """Sealed view of corrected time + sync quality for one evaluation."""

    corrected_utc: datetime
    monotonic_ns: int
    sync_status: TimeSyncStatus
    uncertainty_ms: int
    ready: bool
    reason_code: str | None = None
    estimated_offset_ms: float = 0.0
    snapshot_age_ms: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "corrected_utc",
            require_utc(self.corrected_utc, field_name="corrected_utc"),
        )
        if self.uncertainty_ms < 0:
            raise ValueError("uncertainty_ms must be >= 0")


@runtime_checkable
class TimeAuthority(Protocol):
    """Corrected UTC + monotonic durations + sync readiness."""

    def view(self) -> TimeAuthorityView: ...

    def now_corrected_utc(self) -> datetime: ...

    def monotonic_ns(self) -> int: ...


def _readiness(
    *,
    sync_status: TimeSyncStatus,
    uncertainty_ms: int,
    max_uncertainty_ms: int,
) -> tuple[bool, str | None]:
    if sync_status is TimeSyncStatus.UNSYNCHRONIZED:
        return False, "time_unsynchronized"
    if uncertainty_ms > max_uncertainty_ms:
        return False, "time_uncertainty_exceeded"
    if sync_status is TimeSyncStatus.DEGRADED:
        return False, "time_degraded"
    return True, None


@dataclass
class FakeTimeAuthority:
    """Deterministic offline time authority for tests.

    Wall (corrected UTC) and monotonic clocks advance independently —
    do not mix wall duration with monotonic confirmation duration.
    """

    clock: FakeClock
    uncertainty_ms: int = 0
    sync_status: TimeSyncStatus = TimeSyncStatus.READY
    max_uncertainty_ms: int = 250  # provisional legacy-aligned gate
    estimated_offset_ms: float = 0.0

    def view(self) -> TimeAuthorityView:
        ready, reason = _readiness(
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            max_uncertainty_ms=self.max_uncertainty_ms,
        )
        return TimeAuthorityView(
            corrected_utc=self.clock.now_utc(),
            monotonic_ns=self.clock.monotonic_ns(),
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            ready=ready,
            reason_code=reason,
            estimated_offset_ms=self.estimated_offset_ms,
        )

    def now_corrected_utc(self) -> datetime:
        return self.view().corrected_utc

    def monotonic_ns(self) -> int:
        return self.clock.monotonic_ns()

    def set_uncertainty_ms(self, value: int) -> None:
        if value < 0:
            raise ValueError("uncertainty_ms must be >= 0")
        self.uncertainty_ms = value

    def set_sync_status(self, status: TimeSyncStatus) -> None:
        self.sync_status = status

    def advance(
        self,
        *,
        wall: timedelta | None = None,
        mono_ns: int = 0,
    ) -> None:
        self.clock.advance(wall=wall, mono_ns=mono_ns)

    def set_utc(self, value: datetime) -> None:
        self.clock.set_utc(value)

    def apply_snapshot(self, snapshot: ClockSyncSnapshot) -> None:
        """Test helper: apply sync evidence without network I/O."""
        self.uncertainty_ms = snapshot.uncertainty_ms
        self.sync_status = snapshot.sync_status
        self.estimated_offset_ms = snapshot.estimated_offset_ms


@dataclass
class ClockTimeAuthority:
    """Wrap any Clock as a TimeAuthority with explicit sync metadata."""

    clock: Clock
    uncertainty_ms: int = 0
    sync_status: TimeSyncStatus = TimeSyncStatus.READY
    max_uncertainty_ms: int = 250
    estimated_offset_ms: float = 0.0

    def view(self) -> TimeAuthorityView:
        ready, reason = _readiness(
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            max_uncertainty_ms=self.max_uncertainty_ms,
        )
        return TimeAuthorityView(
            corrected_utc=self.clock.now_utc(),
            monotonic_ns=self.clock.monotonic_ns(),
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            ready=ready,
            reason_code=reason,
            estimated_offset_ms=self.estimated_offset_ms,
        )

    def now_corrected_utc(self) -> datetime:
        return self.view().corrected_utc

    def monotonic_ns(self) -> int:
        return self.clock.monotonic_ns()


@dataclass
class SnapshotTimeAuthority:
    """Core TimeAuthority that interprets ``ClockSyncSnapshot`` evidence only.

    Does not perform network I/O. Without a fresh snapshot, status is
    UNSYNCHRONIZED (or the last applied status if still within validity).
    """

    clock: Clock
    max_uncertainty_ms: int = 250
    max_snapshot_age_ms: int | None = 60_000
    _snapshot: ClockSyncSnapshot | None = field(default=None, init=False, repr=False)

    def apply_snapshot(self, snapshot: ClockSyncSnapshot) -> None:
        self._snapshot = snapshot

    @property
    def last_snapshot(self) -> ClockSyncSnapshot | None:
        return self._snapshot

    def view(self) -> TimeAuthorityView:
        now_wall = self.clock.now_utc()
        mono = self.clock.monotonic_ns()
        snap = self._snapshot
        if snap is None:
            return TimeAuthorityView(
                corrected_utc=now_wall,
                monotonic_ns=mono,
                sync_status=TimeSyncStatus.UNSYNCHRONIZED,
                uncertainty_ms=max(self.max_uncertainty_ms + 1, 1),
                ready=False,
                reason_code="time_unsynchronized",
                estimated_offset_ms=0.0,
                snapshot_age_ms=None,
            )
        age_ms = int((now_wall - snap.measured_at_wall_utc).total_seconds() * 1000.0)
        status = snap.sync_status
        uncertainty = snap.uncertainty_ms
        if self.max_snapshot_age_ms is not None and age_ms > self.max_snapshot_age_ms:
            status = TimeSyncStatus.DEGRADED
            uncertainty = max(uncertainty, age_ms)
        corrected = now_wall + timedelta(milliseconds=snap.estimated_offset_ms)
        if corrected.tzinfo is None:
            corrected = corrected.replace(tzinfo=timezone.utc)
        ready, reason = _readiness(
            sync_status=status,
            uncertainty_ms=uncertainty,
            max_uncertainty_ms=self.max_uncertainty_ms,
        )
        return TimeAuthorityView(
            corrected_utc=corrected.astimezone(timezone.utc),
            monotonic_ns=mono,
            sync_status=status,
            uncertainty_ms=uncertainty,
            ready=ready,
            reason_code=reason,
            estimated_offset_ms=snap.estimated_offset_ms,
            snapshot_age_ms=age_ms,
        )

    def now_corrected_utc(self) -> datetime:
        return self.view().corrected_utc

    def monotonic_ns(self) -> int:
        return self.clock.monotonic_ns()
