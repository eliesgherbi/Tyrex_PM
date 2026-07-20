"""Digital binary fair-value indicator (reusable — no Z-Gap thresholds).

τ_eff = max(τ, τ_floor)
z = ln(S/K) / (σ · √τ_eff)
p_UP = Φ(z)
p_DOWN = 1 − p_UP

σ units: per √second. Float conversion is explicit for log/CDF only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.core.numerics import as_decimal
from tyrex_pm.indicators.ewma_volatility import (
    SIGMA_UNITS_PER_SQRT_SECOND,
    VolatilitySnapshot,
)

MODEL_STATUS_READY = "ready"
MODEL_STATUS_NOT_READY = "not_ready"


def normal_cdf(z: float) -> float:
    """Standard normal CDF Φ(z)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(frozen=True)
class FairValueInput:
    S: Decimal | None
    K: Decimal | None
    sigma: float | None
    tau_s: float | None
    sigma_units: str = SIGMA_UNITS_PER_SQRT_SECOND
    tau_floor_s: float = 1.0
    snapshot_ts: datetime | None = None


@dataclass(frozen=True)
class FairValueSnapshot:
    S: Decimal | None
    K: Decimal | None
    tau_s: float | None
    sigma: float | None
    sigma_units: str
    z: float | None
    p_up: float | None
    p_down: float | None
    model_status: str
    reject_reason: str | None
    snapshot_ts: datetime


def compute_fair_value(
    inp: FairValueInput,
    *,
    vol: VolatilitySnapshot | None = None,
) -> FairValueSnapshot:
    """Compute z and digital probabilities with structured not-ready results."""
    ts = inp.snapshot_ts or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)

    def _not_ready(reason: str) -> FairValueSnapshot:
        return FairValueSnapshot(
            S=inp.S,
            K=inp.K,
            tau_s=inp.tau_s,
            sigma=inp.sigma,
            sigma_units=inp.sigma_units,
            z=None,
            p_up=None,
            p_down=None,
            model_status=MODEL_STATUS_NOT_READY,
            reject_reason=reason,
            snapshot_ts=ts,
        )

    if inp.S is None:
        return _not_ready("missing_binance_price")
    if inp.K is None:
        return _not_ready("missing_ptb_k")
    if inp.tau_s is None:
        return _not_ready("missing_tau")
    if inp.sigma is None:
        return _not_ready("missing_sigma")
    if vol is not None and not vol.ready:
        return _not_ready(vol.reject_reason or "sigma_not_ready")

    s = as_decimal(inp.S, field_name="S")
    k = as_decimal(inp.K, field_name="K")
    if s <= 0:
        return _not_ready("invalid_s")
    if k <= 0:
        return _not_ready("invalid_k")
    if inp.tau_s <= 0:
        return _not_ready("invalid_tau")
    if inp.sigma <= 0:
        return _not_ready("invalid_sigma")
    if inp.sigma_units != SIGMA_UNITS_PER_SQRT_SECOND:
        return _not_ready("unsupported_sigma_units")
    if inp.tau_floor_s <= 0:
        return _not_ready("invalid_tau_floor")

    tau_eff = max(float(inp.tau_s), float(inp.tau_floor_s))
    denom = inp.sigma * math.sqrt(tau_eff)
    if denom <= 0 or not math.isfinite(denom):
        return _not_ready("invalid_sigma_tau")

    # Explicit float conversion for transcendental math only.
    z = math.log(float(s / k)) / denom
    if not math.isfinite(z):
        return _not_ready("non_finite_z")
    # Clamp extreme z for CDF numerical sanity (still compute Φ).
    z_for_cdf = max(min(z, 40.0), -40.0)
    p_up = normal_cdf(z_for_cdf)
    p_up = max(0.0, min(1.0, p_up))
    p_down = 1.0 - p_up

    return FairValueSnapshot(
        S=s,
        K=k,
        tau_s=float(inp.tau_s),
        sigma=float(inp.sigma),
        sigma_units=inp.sigma_units,
        z=z,
        p_up=p_up,
        p_down=p_down,
        model_status=MODEL_STATUS_READY,
        reject_reason=None,
        snapshot_ts=ts,
    )
