"""Time-authority contract for model evaluation (offline-first).

Extends Clock concepts without competing wall-clock systems.
F2 provides FakeTimeAuthority only — no SNTP/HTTP network sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from tyrex_pm.core.clock import Clock, FakeClock, require_utc


class TimeSyncStatus(str, Enum):
    READY = "READY"
    UNSYNCHRONIZED = "UNSYNCHRONIZED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, kw_only=True)
class TimeAuthorityView:
    """Sealed view of corrected time + sync quality for one evaluation."""

    corrected_utc: datetime
    monotonic_ns: int
    sync_status: TimeSyncStatus
    uncertainty_ms: int
    ready: bool
    reason_code: str | None = None

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

    def view(self) -> TimeAuthorityView:
        ready = (
            self.sync_status is TimeSyncStatus.READY
            and self.uncertainty_ms <= self.max_uncertainty_ms
        )
        reason: str | None = None
        if self.sync_status is TimeSyncStatus.UNSYNCHRONIZED:
            reason = "time_unsynchronized"
            ready = False
        elif self.uncertainty_ms > self.max_uncertainty_ms:
            reason = "time_uncertainty_exceeded"
            ready = False
        elif self.sync_status is TimeSyncStatus.DEGRADED:
            reason = "time_degraded"
            ready = False
        return TimeAuthorityView(
            corrected_utc=self.clock.now_utc(),
            monotonic_ns=self.clock.monotonic_ns(),
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            ready=ready,
            reason_code=reason,
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


@dataclass
class ClockTimeAuthority:
    """Wrap any Clock as a TimeAuthority with explicit sync metadata."""

    clock: Clock
    uncertainty_ms: int = 0
    sync_status: TimeSyncStatus = TimeSyncStatus.READY
    max_uncertainty_ms: int = 250

    def view(self) -> TimeAuthorityView:
        ready = (
            self.sync_status is TimeSyncStatus.READY
            and self.uncertainty_ms <= self.max_uncertainty_ms
        )
        reason: str | None = None
        if not ready:
            if self.sync_status is TimeSyncStatus.UNSYNCHRONIZED:
                reason = "time_unsynchronized"
            elif self.uncertainty_ms > self.max_uncertainty_ms:
                reason = "time_uncertainty_exceeded"
            else:
                reason = "time_degraded"
        return TimeAuthorityView(
            corrected_utc=self.clock.now_utc(),
            monotonic_ns=self.clock.monotonic_ns(),
            sync_status=self.sync_status,
            uncertainty_ms=self.uncertainty_ms,
            ready=ready,
            reason_code=reason,
        )

    def now_corrected_utc(self) -> datetime:
        return self.view().corrected_utc

    def monotonic_ns(self) -> int:
        return self.clock.monotonic_ns()
