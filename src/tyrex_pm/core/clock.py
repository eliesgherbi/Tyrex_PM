"""Wall-clock and monotonic time contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


class NaiveDateTimeError(ValueError):
    """Raised when a naive datetime is supplied where UTC is required."""


def require_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    """Accept only timezone-aware UTC datetimes."""
    if value.tzinfo is None:
        raise NaiveDateTimeError(f"{field_name} must be timezone-aware UTC, got naive datetime")
    # Normalize to UTC
    as_utc = value.astimezone(timezone.utc)
    return as_utc


@runtime_checkable
class Clock(Protocol):
    def now_utc(self) -> datetime:
        """Wall-clock UTC time for venue timestamps and reporting."""

    def monotonic_ns(self) -> int:
        """Monotonic nanoseconds for elapsed durations and deadlines."""


@dataclass(slots=True)
class SystemClock:
    """Production clock using the host OS."""

    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic_ns(self) -> int:
        import time

        return time.monotonic_ns()


@dataclass(slots=True)
class FakeClock:
    """Deterministic clock for tests."""

    _wall: datetime
    _mono_ns: int = 0
    _wall_step: timedelta = field(default_factory=lambda: timedelta(0))
    _mono_step_ns: int = 0

    def __post_init__(self) -> None:
        self._wall = require_utc(self._wall, field_name="FakeClock initial wall time")

    def now_utc(self) -> datetime:
        current = self._wall
        if self._wall_step:
            self._wall = self._wall + self._wall_step
        return current

    def monotonic_ns(self) -> int:
        current = self._mono_ns
        if self._mono_step_ns:
            self._mono_ns += self._mono_step_ns
        return current

    def set_utc(self, value: datetime) -> None:
        self._wall = require_utc(value, field_name="FakeClock.set_utc")

    def advance(self, *, wall: timedelta | None = None, mono_ns: int = 0) -> None:
        if wall is not None:
            self._wall = self._wall + wall
        if mono_ns:
            self._mono_ns += mono_ns
