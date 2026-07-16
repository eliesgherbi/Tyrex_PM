"""Binary fair-value model for BTC 5m digital contracts (A0.3)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from tyrex_pm.quant.volatility import SIGMA_UNITS_PER_SQRT_SECOND, SigmaConfig, VolatilitySnapshot

MODEL_STATUS_READY = "ready"
MODEL_STATUS_NOT_READY = "not_ready"


def normal_cdf(z: float) -> float:
    """Standard normal CDF Phi(z)."""
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def compute_fair_value(
    inp: FairValueInput,
    *,
    vol: VolatilitySnapshot | None = None,
) -> FairValueSnapshot:
    """Compute z and digital probabilities with structured not-ready results."""
    ts = inp.snapshot_ts or _utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

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
        reason = vol.reject_reason or "sigma_not_ready"
        return _not_ready(reason)
    if inp.S <= 0:
        return _not_ready("invalid_s")
    if inp.K <= 0:
        return _not_ready("invalid_k")
    if inp.tau_s <= 0:
        return _not_ready("invalid_tau")
    if inp.sigma <= 0:
        return _not_ready("invalid_sigma")
    if inp.sigma_units != SIGMA_UNITS_PER_SQRT_SECOND:
        return _not_ready("unsupported_sigma_units")

    tau_eff = max(float(inp.tau_s), float(inp.tau_floor_s))
    denom = inp.sigma * math.sqrt(tau_eff)
    if denom <= 0:
        return _not_ready("invalid_sigma_tau")

    z = math.log(float(inp.S / inp.K)) / denom
    p_up = normal_cdf(z)
    p_down = 1.0 - p_up

    return FairValueSnapshot(
        S=inp.S,
        K=inp.K,
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


def default_sigma_config() -> SigmaConfig:
    return SigmaConfig()
