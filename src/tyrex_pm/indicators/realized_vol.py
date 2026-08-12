"""Short-horizon realized volatility from log returns."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from math import log, sqrt


@dataclass
class RollingRealizedVol:
    horizon: timedelta
    _events: deque[tuple[datetime, float]] = field(default_factory=deque)
    _last_price: Decimal | None = None
    _sum_sq: float = 0.0

    def update(self, *, price: Decimal, ts_event: datetime) -> float | None:
        if price <= 0:
            return self.value
        if self._last_price is not None and self._last_price > 0:
            ret = log(float(price / self._last_price))
            self._events.append((ts_event, ret * ret))
            self._sum_sq += ret * ret
        self._last_price = price
        cutoff = ts_event - self.horizon
        while self._events and self._events[0][0] < cutoff:
            _, old = self._events.popleft()
            self._sum_sq -= old
        return self.value

    @property
    def value(self) -> float | None:
        if not self._events:
            return None
        return sqrt(max(0.0, self._sum_sq))
