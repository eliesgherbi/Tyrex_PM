"""Feed diagnostics: BTC reference freshness, separate from socket liveness.

"Messages are arriving" (the adapter's heartbeat) and "fresh BTC reference prices are
arriving" are different checks: a socket can stay open while the subscribed series goes
quiet. This watchdog tracks only accepted BTC reference ticks and reports each stall
once, with its start, and each resumption with the measured gap, so reconnect thresholds
can be tuned from evidence instead of guessed.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


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


@dataclass
class EventLoopLagMonitor:
    """Measures how late the event loop wakes a periodic task (local scheduling delay).

    Separate network clients still share one process: if the loop is busy, every
    client waits. Lag here separates that from exchange or network latency.
    """

    interval_s: float = 0.1
    report_above_ms: float = 250.0
    report_every_s: float = 5.0
    on_report: Callable[[str, dict], None] | None = None
    _samples: deque = field(default_factory=lambda: deque(maxlen=600), init=False)
    _task: asyncio.Task | None = field(default=None, init=False)
    _last_report_s: float = field(default=0.0, init=False)
    max_lag_ms: float = field(default=0.0, init=False)
    # Distribution of every sample, reported once per interval (overhead measurement).
    summary_every_s: float = 60.0
    _last_summary_s: float = field(default=0.0, init=False)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="event-loop-lag")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def record(self, lag_ms: float, now_s: float) -> None:
        self._samples.append((now_s, lag_ms))
        self.max_lag_ms = max(self.max_lag_ms, lag_ms)
        if self._last_summary_s == 0.0:
            self._last_summary_s = now_s
        elif now_s - self._last_summary_s >= self.summary_every_s and self.on_report:
            window = sorted(lag for at, lag in self._samples
                            if now_s - at <= self.summary_every_s)
            self._last_summary_s = now_s
            if window:
                def pct(q: float) -> float:
                    return round(window[min(len(window) - 1, int(q * len(window)))], 2)

                try:
                    self.on_report("EVENT_LOOP_LAG_SUMMARY", {
                        "window_s": self.summary_every_s, "samples": len(window),
                        "p50_ms": pct(0.5), "p95_ms": pct(0.95), "p99_ms": pct(0.99),
                        "max_ms": round(window[-1], 2)})
                except Exception:  # noqa: BLE001, S110 - diagnostics never stop trading
                    pass
        if (
            lag_ms > self.report_above_ms
            and self.on_report is not None
            and now_s - self._last_report_s >= self.report_every_s
        ):
            self._last_report_s = now_s
            try:
                self.on_report("EVENT_LOOP_LAG", {"lag_ms": round(lag_ms, 1),
                                                  "recent_max_ms": self.recent_max_ms(now_s)})
            except Exception:  # noqa: BLE001, S110 - diagnostics never stop trading
                pass

    def recent_max_ms(self, now_s: float | None = None, window_s: float = 10.0) -> float:
        now = time.monotonic() if now_s is None else now_s
        recent = [lag for at, lag in self._samples if now - at <= window_s]
        return round(max(recent, default=0.0), 1)

    async def _run(self) -> None:
        while True:
            expected = time.monotonic() + self.interval_s
            await asyncio.sleep(self.interval_s)
            now = time.monotonic()
            self.record(max(0.0, (now - expected) * 1_000), now)
