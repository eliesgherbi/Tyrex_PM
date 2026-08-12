"""Cont-style L1 order-flow imbalance on successive DepthSnapshots."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from tyrex_pm.indicators.depth import DepthSnapshot


def l1_ofi_delta(previous: DepthSnapshot | None, current: DepthSnapshot) -> Decimal:
    """Signed L1 OFI contribution of one book update."""
    if previous is None:
        return Decimal("0")
    ofi = Decimal("0")
    pb, cb = previous.best_bid, current.best_bid
    if cb is not None:
        if pb is None or cb.price > pb.price:
            ofi += cb.quantity
        elif cb.price == pb.price:
            ofi += cb.quantity - pb.quantity
        else:
            ofi -= pb.quantity
    elif pb is not None:
        ofi -= pb.quantity
    pa, ca = previous.best_ask, current.best_ask
    if ca is not None:
        if pa is None or ca.price < pa.price:
            ofi -= ca.quantity
        elif ca.price == pa.price:
            ofi -= ca.quantity - pa.quantity
        else:
            ofi += pa.quantity
    elif pa is not None:
        ofi += pa.quantity
    return ofi


@dataclass
class RollingOfi:
    """Rolling sum of L1 OFI over a horizon."""

    horizon: timedelta
    _events: deque[tuple[datetime, Decimal]] = field(default_factory=deque)
    _previous: DepthSnapshot | None = None
    _sum: Decimal = Decimal("0")

    def update(self, snapshot: DepthSnapshot) -> Decimal:
        delta = l1_ofi_delta(self._previous, snapshot)
        self._previous = snapshot
        ts = snapshot.ts_event
        self._events.append((ts, delta))
        self._sum += delta
        cutoff = ts - self.horizon
        while self._events and self._events[0][0] < cutoff:
            _, old = self._events.popleft()
            self._sum -= old
        return self._sum

    @property
    def value(self) -> Decimal:
        return self._sum
