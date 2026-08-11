"""Short-horizon return momentum on Binance reference prices.

Formula:
    m_t = P_t / P_{t-L} - 1

where P_t is the latest price and P_{t-L} is the newest observation with
``ts_event <= P_t.ts_event - lookback`` (no interpolation).

Insufficient history → value None / ready=False.
Out-of-order observations with ts_event < last accepted are ignored.
Duplicate timestamps replace the previous sample at that timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import EventId
from tyrex_pm.core.indicators import IndicatorResult
from tyrex_pm.core.snapshots import ReferencePriceSnapshot


@dataclass(frozen=True, kw_only=True)
class MomentumConfig:
    lookback: timedelta
    min_samples: int = 2

    def __post_init__(self) -> None:
        if self.lookback <= timedelta(0):
            raise ValueError("lookback must be > 0")
        if self.min_samples < 2:
            raise ValueError("min_samples must be >= 2")


@dataclass
class _Sample:
    ts_event: datetime
    price: Decimal


class ShortHorizonMomentum:
    def __init__(self, config: MomentumConfig) -> None:
        self._cfg = config
        self._samples: list[_Sample] = []

    def reset(self) -> None:
        self._samples.clear()

    def update(
        self,
        snapshot: ReferencePriceSnapshot,
        *,
        source_event_id: EventId | None = None,
    ) -> IndicatorResult:
        ts = require_utc(snapshot.ts_event, field_name="ts_event")
        if self._samples and ts < self._samples[-1].ts_event:
            # Ignore out-of-order
            return self._result(
                ts, None, ready=False, source_event_id=source_event_id, reason="OUT_OF_ORDER"
            )
        if self._samples and ts == self._samples[-1].ts_event:
            self._samples[-1] = _Sample(ts_event=ts, price=snapshot.price)
        else:
            self._samples.append(_Sample(ts_event=ts, price=snapshot.price))
        # Prune older than 2x lookback for memory
        cutoff = ts - self._cfg.lookback * 2
        self._samples = [s for s in self._samples if s.ts_event >= cutoff]

        if len(self._samples) < self._cfg.min_samples:
            return self._result(
                ts,
                None,
                ready=False,
                source_event_id=source_event_id,
                reason="INSUFFICIENT_SAMPLES",
            )

        target = ts - self._cfg.lookback
        baseline = None
        for sample in reversed(self._samples[:-1]):
            if sample.ts_event <= target:
                baseline = sample
                break
        if baseline is None:
            return self._result(
                ts,
                None,
                ready=False,
                source_event_id=source_event_id,
                reason="INSUFFICIENT_HISTORY",
            )
        if baseline.price == 0:
            return self._result(
                ts, None, ready=False, source_event_id=source_event_id, reason="ZERO_BASELINE"
            )
        momentum = snapshot.price / baseline.price - Decimal("1")
        return self._result(ts, momentum, ready=True, source_event_id=source_event_id, reason="OK")

    @staticmethod
    def _result(
        observed_at: datetime,
        value: Decimal | None,
        *,
        ready: bool,
        source_event_id: EventId | None,
        reason: str,
    ) -> IndicatorResult:
        return IndicatorResult(
            indicator_id="short_horizon_momentum",
            observed_at=observed_at,
            value={"momentum": value, "ready": ready, "reason": reason},
            source_event_id=source_event_id,
        )
