"""EWMA volatility estimator for Z-Gap and related strategies (A0.3)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

SIGMA_UNITS_PER_SQRT_SECOND = "per_sqrt_second"
ESTIMATOR_EWMA = "ewma"


@dataclass(frozen=True)
class SigmaConfig:
    """EWMA sigma configuration (Z-Gap v0 defaults)."""

    estimator: str = ESTIMATOR_EWMA
    half_life_s: float = 30.0
    min_samples_s: float = 20.0
    jump_guard: bool = True
    jump_threshold_sigma: float = 4.0
    sample_interval_s: float = 1.0
    tau_floor_s: float = 1.0

    def ewma_lambda(self) -> float:
        if self.half_life_s <= 0:
            raise ValueError("half_life_s must be positive")
        return math.exp(-math.log(2.0) / self.half_life_s)


@dataclass(frozen=True)
class VolatilitySnapshot:
    """Point-in-time EWMA volatility state."""

    sigma: float | None
    sigma_units: str
    ready: bool
    sample_count: int
    effective_samples_s: float
    last_update_ts: datetime | None
    jump_guard_tripped: bool
    reject_reason: str | None


@dataclass
class EwmaVolatilityEstimator:
    """EWMA variance on log returns with explicit per-sqrt-second sigma units.

    Update policy:
    - Resample to ``sample_interval_s`` buckets using the latest price in each bucket.
    - ``r_t = ln(S_t / S_{t-1})`` over actual elapsed ``dt_s`` between accepted samples.
    - ``var_t = lambda * var_{t-1} + (1 - lambda) * r_t^2``
    - ``sigma_t = sqrt(var_t / dt_s)`` → per sqrt(second).
    - Jump guard: if ``|r_t| > jump_threshold_sigma * sigma_{t-1}``, skip the variance
      update, set ``jump_guard_tripped=True``, and keep the prior sigma.
    """

    config: SigmaConfig = field(default_factory=SigmaConfig)
    _var: float | None = field(default=None, init=False, repr=False)
    _sigma: float | None = field(default=None, init=False, repr=False)
    _last_price: Decimal | None = field(default=None, init=False, repr=False)
    _last_sample_ts: datetime | None = field(default=None, init=False, repr=False)
    _pending_price: Decimal | None = field(default=None, init=False, repr=False)
    _pending_ts: datetime | None = field(default=None, init=False, repr=False)
    _sample_count: int = field(default=0, init=False, repr=False)
    _effective_samples_s: float = field(default=0.0, init=False, repr=False)
    _last_update_ts: datetime | None = field(default=None, init=False, repr=False)
    _jump_guard_tripped: bool = field(default=False, init=False, repr=False)

    @property
    def sigma_units(self) -> str:
        return SIGMA_UNITS_PER_SQRT_SECOND

    def _normalize_ts(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts

    def _snapshot(
        self,
        *,
        jump_guard_tripped: bool | None = None,
        reject_reason: str | None = None,
    ) -> VolatilitySnapshot:
        tripped = self._jump_guard_tripped if jump_guard_tripped is None else jump_guard_tripped
        ready = (
            self._sigma is not None
            and self._effective_samples_s >= self.config.min_samples_s
            and not tripped
        )
        reason = reject_reason
        if not ready and reason is None:
            if tripped:
                reason = "jump_guard_tripped"
            elif self._sigma is None:
                reason = "warming_up"
            elif self._effective_samples_s < self.config.min_samples_s:
                reason = "min_samples_not_met"
        return VolatilitySnapshot(
            sigma=self._sigma,
            sigma_units=self.sigma_units,
            ready=ready,
            sample_count=self._sample_count,
            effective_samples_s=round(self._effective_samples_s, 6),
            last_update_ts=self._last_update_ts,
            jump_guard_tripped=tripped,
            reject_reason=reason,
        )

    def snapshot(self) -> VolatilitySnapshot:
        return self._snapshot()

    def update(self, price: Decimal, ts: datetime) -> VolatilitySnapshot:
        """Ingest a Binance price observation (any cadence); buckets by ``sample_interval_s``."""
        if price <= 0:
            return self._snapshot(reject_reason="invalid_price")

        ts = self._normalize_ts(ts)
        self._pending_price = price
        self._pending_ts = ts

        if self._last_sample_ts is None:
            self._last_price = price
            self._last_sample_ts = ts
            return self._snapshot(reject_reason="warming_up")

        elapsed_s = (ts - self._last_sample_ts).total_seconds()
        if elapsed_s < self.config.sample_interval_s:
            return self.snapshot()

        dt_s = elapsed_s
        if dt_s <= 0:
            return self.snapshot()

        new_price = self._pending_price or price
        old_price = self._last_price
        if old_price is None or old_price <= 0:
            self._last_price = new_price
            self._last_sample_ts = ts
            return self._snapshot(reject_reason="warming_up")

        r_t = math.log(float(new_price / old_price))
        lam = self.config.ewma_lambda()
        jump_tripped = False

        if self._sigma is not None and self.config.jump_guard and self._sigma > 0:
            threshold = self.config.jump_threshold_sigma * self._sigma
            if abs(r_t) > threshold:
                self._jump_guard_tripped = True
                jump_tripped = True
                self._last_price = new_price
                self._last_sample_ts = ts
                return self._snapshot(jump_guard_tripped=True, reject_reason="jump_guard_tripped")

        if self._var is None:
            self._var = r_t * r_t
        else:
            self._var = lam * self._var + (1.0 - lam) * (r_t * r_t)

        self._sigma = math.sqrt(self._var / dt_s)
        self._sample_count += 1
        self._effective_samples_s += dt_s
        self._last_update_ts = ts
        self._last_price = new_price
        self._last_sample_ts = ts
        self._jump_guard_tripped = False

        return self._snapshot(jump_guard_tripped=jump_tripped)

    def reset(self) -> None:
        self._var = None
        self._sigma = None
        self._last_price = None
        self._last_sample_ts = None
        self._pending_price = None
        self._pending_ts = None
        self._sample_count = 0
        self._effective_samples_s = 0.0
        self._last_update_ts = None
        self._jump_guard_tripped = False
