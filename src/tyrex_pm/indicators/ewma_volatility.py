"""EWMA volatility estimator (reusable indicator — no Z-Gap thresholds).

Semantics ported from audited legacy Phase A evidence (read-only).
Sigma units: per √second.

Defaults are **provisional** (legacy-aligned) unless overridden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.numerics import as_decimal

SIGMA_UNITS_PER_SQRT_SECOND = "per_sqrt_second"
ESTIMATOR_EWMA = "ewma"

# Conservative BTC 1s bootstrap floor (~daily 3% → per √s). Prevents micro-σ
# jump thresholds that lock the estimator for an entire window.
DEFAULT_SIGMA_FLOOR = 1e-4


@dataclass(frozen=True)
class SigmaConfig:
    """EWMA sigma configuration.

    Fields marked provisional retain audited legacy Phase A defaults.
    """

    estimator: str = ESTIMATOR_EWMA
    half_life_s: float = 30.0  # provisional
    min_samples_s: float = 20.0  # provisional
    jump_guard: bool = True
    jump_threshold_sigma: float = 4.0  # provisional
    sample_interval_s: float = 1.0  # provisional default 1s resample
    tau_floor_s: float = 1.0  # provisional (used by fair-value consumers)
    sigma_floor: float = DEFAULT_SIGMA_FLOOR  # provisional per √second

    def ewma_lambda(self) -> float:
        if self.half_life_s <= 0:
            raise ValueError("half_life_s must be positive")
        return math.exp(-math.log(2.0) / self.half_life_s)

    def validate(self) -> None:
        if self.half_life_s <= 0:
            raise ValueError("half_life_s must be positive")
        if self.min_samples_s < 0:
            raise ValueError("min_samples_s must be >= 0")
        if self.jump_threshold_sigma <= 0:
            raise ValueError("jump_threshold_sigma must be positive")
        if self.sample_interval_s <= 0:
            raise ValueError("sample_interval_s must be positive")
        if self.tau_floor_s <= 0:
            raise ValueError("tau_floor_s must be positive")
        if self.sigma_floor < 0:
            raise ValueError("sigma_floor must be >= 0")


@dataclass(frozen=True)
class SeedResult:
    accepted: int
    rejected_duplicate: int
    rejected_future: int
    rejected_out_of_order: int
    start_ts: datetime | None
    end_ts: datetime | None


@dataclass(frozen=True)
class VolatilitySnapshot:
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
    """EWMA variance on log returns with explicit per-√second sigma units.

    Update policy:
    - Resample to ``sample_interval_s`` buckets using the latest price in each bucket.
    - ``r_t = ln(S_t / S_{t-1})`` over actual elapsed ``dt_s``.
    - ``var_t = λ · var_{t-1} + (1 − λ) · r_t²``
    - ``σ_t = max(√(var_t / dt_s), sigma_floor)`` → per √second.
    - Jump guard uses the floored σ for the threshold. A trip marks the current
      sample not-ready but still updates variance so the estimator can recover.
    - Near-zero returns do not install σ=0; the estimator stays warming until a
      non-zero return establishes variance (then floored).
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

    def __post_init__(self) -> None:
        self.config.validate()

    @property
    def sigma_units(self) -> str:
        return SIGMA_UNITS_PER_SQRT_SECOND

    def _normalize_ts(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)

    def _floored_sigma(self, sigma: float) -> float:
        floor = self.config.sigma_floor
        if floor <= 0:
            return sigma
        return max(sigma, floor)

    def _jump_threshold(self) -> float | None:
        if self._sigma is None:
            return None
        return self.config.jump_threshold_sigma * self._floored_sigma(self._sigma)

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

    def _commit_variance(self, *, r_t: float, dt_s: float, ts: datetime, price: Decimal) -> None:
        lam = self.config.ewma_lambda()
        if self._var is None:
            self._var = r_t * r_t
        else:
            self._var = lam * self._var + (1.0 - lam) * (r_t * r_t)
        self._sigma = self._floored_sigma(math.sqrt(self._var / dt_s))
        self._sample_count += 1
        self._effective_samples_s += dt_s
        self._last_update_ts = ts
        self._last_price = price
        self._last_sample_ts = ts

    def update(self, price: Decimal | str | int, ts: datetime) -> VolatilitySnapshot:
        price_d = as_decimal(price, field_name="price")
        if price_d <= 0:
            return self._snapshot(reject_reason="invalid_price")

        ts = self._normalize_ts(ts)
        self._pending_price = price_d
        self._pending_ts = ts

        if self._last_sample_ts is None:
            self._last_price = price_d
            self._last_sample_ts = ts
            return self._snapshot(reject_reason="warming_up")

        elapsed_s = (ts - self._last_sample_ts).total_seconds()
        if elapsed_s < self.config.sample_interval_s:
            return self.snapshot()

        dt_s = elapsed_s
        if dt_s <= 0:
            return self.snapshot()

        new_price = self._pending_price or price_d
        old_price = self._last_price
        if old_price is None or old_price <= 0:
            self._last_price = new_price
            self._last_sample_ts = ts
            return self._snapshot(reject_reason="warming_up")

        r_t = math.log(float(new_price / old_price))
        if abs(r_t) < 1e-15:
            self._last_price = new_price
            self._last_sample_ts = ts
            # Do not install σ=0 from stagnant prices; that made every later
            # move look like a jump and froze readiness for the whole window.
            if self._sigma is not None:
                self._effective_samples_s += dt_s
            return self.snapshot()

        jump_tripped = False
        threshold = self._jump_threshold()
        if (
            threshold is not None
            and self.config.jump_guard
            and abs(r_t) > threshold
        ):
            jump_tripped = True
            self._jump_guard_tripped = True
            # Still update variance so σ can adapt after an outlier.
            self._commit_variance(r_t=r_t, dt_s=dt_s, ts=ts, price=new_price)
            return self._snapshot(jump_guard_tripped=True, reject_reason="jump_guard_tripped")

        self._commit_variance(r_t=r_t, dt_s=dt_s, ts=ts, price=new_price)
        self._jump_guard_tripped = False
        return self._snapshot(jump_guard_tripped=False)

    def seed_observations(
        self,
        observations: list[tuple[Decimal | str | int, datetime]],
        *,
        now_ts: datetime | None = None,
    ) -> SeedResult:
        now = self._normalize_ts(now_ts) if now_ts is not None else None
        accepted = 0
        rejected_duplicate = 0
        rejected_future = 0
        rejected_out_of_order = 0
        start_ts: datetime | None = None
        end_ts: datetime | None = None
        last_batch_ts: datetime | None = None

        for price, raw_ts in sorted(observations, key=lambda row: row[1]):
            ts = self._normalize_ts(raw_ts)
            if now is not None and ts > now:
                rejected_future += 1
                continue
            if last_batch_ts is not None and ts == last_batch_ts:
                rejected_duplicate += 1
                continue
            if self._last_sample_ts is not None and ts < self._last_sample_ts:
                rejected_out_of_order += 1
                continue
            last_batch_ts = ts
            self.update(price, ts)
            accepted += 1
            if start_ts is None:
                start_ts = ts
            end_ts = ts

        return SeedResult(
            accepted=accepted,
            rejected_duplicate=rejected_duplicate,
            rejected_future=rejected_future,
            rejected_out_of_order=rejected_out_of_order,
            start_ts=start_ts,
            end_ts=end_ts,
        )

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


def default_sigma_config() -> SigmaConfig:
    return SigmaConfig()
