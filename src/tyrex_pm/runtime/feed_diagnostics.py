"""Feed diagnostics: BTC reference freshness, separate from socket liveness.

"Messages are arriving" (the adapter's heartbeat) and "fresh BTC reference prices are
arriving" are different checks: a socket can stay open while the subscribed series goes
quiet. This watchdog tracks only accepted BTC reference ticks and reports each stall
once, with its start, and each resumption with the measured gap, so reconnect thresholds
can be tuned from evidence instead of guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ReferenceFreshnessWatchdog:
    # Provisional: the reference publishes about once per second.
    stale_after_s: float = 3.0
    _last_mono_s: float | None = field(default=None, init=False)
    _last_source_ts: datetime | None = field(default=None, init=False)
    _stale_since_mono_s: float | None = field(default=None, init=False)

    def on_tick(self, *, source_ts: datetime, receive_mono_s: float) -> list[tuple[str, dict]]:
        """Record an accepted reference tick; returns a resumption event after a stall."""
        events: list[tuple[str, dict[str, Any]]] = []
        if self._stale_since_mono_s is not None and self._last_mono_s is not None:
            events.append(
                (
                    "REFERENCE_FRESH_AGAIN",
                    {
                        "gap_s": round(receive_mono_s - self._last_mono_s, 3),
                        "last_source_ts_before_gap": None
                        if self._last_source_ts is None
                        else self._last_source_ts.isoformat(),
                        "first_source_ts_after_gap": source_ts.isoformat(),
                    },
                )
            )
            self._stale_since_mono_s = None
        self._last_mono_s = receive_mono_s
        self._last_source_ts = source_ts
        return events

    def check(self, now_mono_s: float) -> list[tuple[str, dict]]:
        """Report the start of a stall once (no reference tick for ``stale_after_s``)."""
        if self._last_mono_s is None or self._stale_since_mono_s is not None:
            return []
        age = now_mono_s - self._last_mono_s
        if age <= self.stale_after_s:
            return []
        self._stale_since_mono_s = now_mono_s
        return [
            (
                "REFERENCE_STALE",
                {
                    "age_s": round(age, 3),
                    "stale_after_s": self.stale_after_s,
                    "last_source_ts": None
                    if self._last_source_ts is None
                    else self._last_source_ts.isoformat(),
                },
            )
        ]
