"""Bounded, sampled price path of a trade stream (reusable; no strategy thresholds).

The path is piecewise constant: each sample interval keeps the last trade price seen in
it, and that price holds until the next sampled change. ``path_mean`` integrates the
step function over a time range and reports how much of the range was covered, so a
caller can refuse a mean built on missing history instead of treating gaps as zero.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal


@dataclass(frozen=True)
class SpotPathSnapshot:
    ready: bool
    reject_reason: str | None
    sample_interval_s: float
    window_s: float
    # (bucket start epoch seconds, last price in that bucket), chronological.
    samples: tuple[tuple[float, Decimal], ...]
    last_price: Decimal | None
    last_ts: datetime | None


def empty_spot_path(*, reject_reason: str = "warming_up", interval_s: float = 1.0,
                    window_s: float = 0.0) -> SpotPathSnapshot:
    return SpotPathSnapshot(
        ready=False, reject_reason=reject_reason, sample_interval_s=interval_s,
        window_s=window_s, samples=(), last_price=None, last_ts=None,
    )


class RollingSpotPath:
    def __init__(self, *, window_s: float, sample_interval_s: float = 1.0) -> None:
        if window_s <= 0 or sample_interval_s <= 0:
            raise ValueError("window_s and sample_interval_s must be > 0")
        self.window_s = float(window_s)
        self.interval = float(sample_interval_s)
        self._keep = math.ceil(self.window_s / self.interval) + 1
        self._buckets: list[int] = []
        self._samples: list[tuple[float, Decimal]] = []
        self._last_ts: datetime | None = None

    def update(self, price: Decimal, ts_event: datetime) -> SpotPathSnapshot:
        bucket = math.floor(ts_event.timestamp() / self.interval)
        if self._buckets and bucket < self._buckets[-1]:
            return self.snapshot()  # out-of-order print: never rewrites sampled history
        if self._buckets and bucket == self._buckets[-1]:
            self._samples[-1] = (bucket * self.interval, price)
        else:
            self._buckets.append(bucket)
            self._samples.append((bucket * self.interval, price))
            horizon = bucket - self._keep
            drop = 0
            while drop < len(self._buckets) and self._buckets[drop] < horizon:
                drop += 1
            if drop:
                del self._buckets[:drop]
                del self._samples[:drop]
        self._last_ts = ts_event
        return self.snapshot()

    def snapshot(self) -> SpotPathSnapshot:
        if not self._samples:
            return empty_spot_path(interval_s=self.interval, window_s=self.window_s)
        return SpotPathSnapshot(
            ready=True, reject_reason=None, sample_interval_s=self.interval,
            window_s=self.window_s, samples=tuple(self._samples),
            last_price=self._samples[-1][1], last_ts=self._last_ts,
        )


def path_mean(
    snapshot: SpotPathSnapshot, start: datetime, end: datetime
) -> tuple[Decimal | None, float]:
    """Time-weighted mean of the step path over [start, end] and covered fraction.

    The price sampled in bucket ``b`` applies from the end of that bucket until the
    next sample takes over, so a value is used only after it was observable.
    Coverage below 1 means part of the range precedes the first usable sample.
    """
    t0, t1 = start.astimezone(timezone.utc).timestamp(), end.astimezone(timezone.utc).timestamp()
    if t1 <= t0 or not snapshot.samples:
        return None, 0.0
    steps = [(begin + snapshot.sample_interval_s, price) for begin, price in snapshot.samples]
    total = Decimal(0)
    covered = 0.0
    for i, (since, price) in enumerate(steps):
        until = steps[i + 1][0] if i + 1 < len(steps) else math.inf
        left, right = max(since, t0), min(until, t1)
        if right > left:
            total += price * Decimal(repr(right - left))
            covered += right - left
    if covered <= 0:
        return None, 0.0
    return total / Decimal(repr(covered)), covered / (t1 - t0)
