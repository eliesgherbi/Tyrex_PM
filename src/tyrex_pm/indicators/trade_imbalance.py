"""Aggressive trade imbalance over rolling horizons."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

Aggressor = Literal["buy", "sell"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TradePrint:
    ts_event: datetime
    price: Decimal
    quantity: Decimal
    aggressor: Aggressor


@dataclass
class RollingTradeImbalance:
    horizon: timedelta
    _events: deque[TradePrint] = field(default_factory=deque)
    _buy: Decimal = Decimal("0")
    _sell: Decimal = Decimal("0")

    def ingest(self, print_: TradePrint) -> Decimal | None:
        self._events.append(print_)
        if print_.aggressor == "buy":
            self._buy += print_.quantity
        else:
            self._sell += print_.quantity
        cutoff = print_.ts_event - self.horizon
        while self._events and self._events[0].ts_event < cutoff:
            old = self._events.popleft()
            if old.aggressor == "buy":
                self._buy -= old.quantity
            else:
                self._sell -= old.quantity
        return self.value

    @property
    def value(self) -> Decimal | None:
        total = self._buy + self._sell
        if total <= 0:
            return None
        return (self._buy - self._sell) / total
