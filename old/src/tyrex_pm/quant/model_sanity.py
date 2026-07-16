"""Numerical sanity checks for Z-Gap fair-value model (diagnostics only)."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tyrex_pm.quant.binary_fair_value import FairValueSnapshot, MODEL_STATUS_READY

MODEL_NUMERIC_ANOMALY = "model_numeric_anomaly"

DEFAULT_WARN_ABS_Z = 8.0
DEFAULT_BLOCK_ABS_Z = 20.0
DEFAULT_SIGMA_RATIO_WARN_MIN = 0.01
DEFAULT_SIGMA_RATIO_WARN_MAX = 100.0


@dataclass(frozen=True)
class ModelSanityConfig:
    warn_abs_z: float = DEFAULT_WARN_ABS_Z
    block_abs_z: float = DEFAULT_BLOCK_ABS_Z
    sigma_ratio_warn_min: float = DEFAULT_SIGMA_RATIO_WARN_MIN
    sigma_ratio_warn_max: float = DEFAULT_SIGMA_RATIO_WARN_MAX


@dataclass(frozen=True)
class ModelSanityResult:
    ok: bool
    warn: bool
    block_entry: bool
    issues: tuple[str, ...]
    abs_z: float | None = None
    computed_z: float | None = None


def recompute_z(
    *,
    s: Decimal | float,
    k: Decimal | float,
    sigma: float,
    tau_s: float,
    tau_floor_s: float = 1.0,
) -> float | None:
    if s <= 0 or k <= 0 or sigma <= 0 or tau_s is None:
        return None
    tau_eff = max(float(tau_s), float(tau_floor_s))
    denom = float(sigma) * math.sqrt(tau_eff)
    if denom <= 0 or not math.isfinite(denom):
        return None
    return math.log(float(s) / float(k)) / denom


def simple_realized_sigma_per_sqrt_second(
    observations: list[tuple[Decimal, datetime]],
    *,
    sample_interval_s: float = 1.0,
) -> float | None:
    """Diagnostic-only 1-second bucketed log-return stdev (per sqrt(second))."""
    if len(observations) < 2:
        return None
    ordered = sorted(observations, key=lambda row: row[1])
    returns: list[float] = []
    last_price: Decimal | None = None
    last_ts: datetime | None = None
    for price, ts in ordered:
        if last_price is None or last_ts is None:
            last_price = price
            last_ts = ts
            continue
        elapsed = (ts - last_ts).total_seconds()
        if elapsed < sample_interval_s:
            continue
        if price <= 0 or last_price <= 0:
            last_price = price
            last_ts = ts
            continue
        if price != last_price:
            returns.append(math.log(float(price / last_price)))
        last_price = price
        last_ts = ts
    if len(returns) < 2:
        return None
    return statistics.pstdev(returns)


def sigma_ratio_warning(
    *,
    ewma_sigma: float | None,
    simple_sigma: float | None,
    cfg: ModelSanityConfig,
) -> str | None:
    if ewma_sigma is None or simple_sigma is None:
        return None
    if ewma_sigma <= 0 or simple_sigma <= 0:
        return "sigma_non_positive"
    ratio = ewma_sigma / simple_sigma
    if ratio < cfg.sigma_ratio_warn_min or ratio > cfg.sigma_ratio_warn_max:
        return f"sigma_ratio_out_of_band:{ratio:.6g}"
    return None


def evaluate_model_numeric_sanity(
    fair: FairValueSnapshot,
    *,
    cfg: ModelSanityConfig,
    block_entries: bool,
    tau_floor_s: float = 1.0,
) -> ModelSanityResult:
    issues: list[str] = []

    sigma = fair.sigma
    if sigma is None or not math.isfinite(sigma) or sigma <= 0:
        issues.append("sigma_invalid")

    z = fair.z
    if z is not None and not math.isfinite(z):
        issues.append("z_non_finite")
    if fair.p_up is not None:
        if not math.isfinite(fair.p_up) or fair.p_up < 0 or fair.p_up > 1:
            issues.append("p_up_out_of_range")
    if fair.p_down is not None:
        if not math.isfinite(fair.p_down) or fair.p_down < 0 or fair.p_down > 1:
            issues.append("p_down_out_of_range")

    computed_z: float | None = None
    if fair.S is not None and fair.K is not None and sigma is not None and fair.tau_s is not None:
        computed_z = recompute_z(
            s=fair.S,
            k=fair.K,
            sigma=float(sigma),
            tau_s=float(fair.tau_s),
            tau_floor_s=tau_floor_s,
        )
        if computed_z is not None and z is not None and math.isfinite(computed_z) and math.isfinite(z):
            if abs(computed_z - z) > max(1e-6, abs(z) * 1e-6):
                issues.append("z_formula_mismatch")

    abs_z = abs(z) if z is not None and math.isfinite(z) else None
    warn = False
    block = False
    if abs_z is not None and abs_z > cfg.warn_abs_z:
        issues.append("abs_z_warn")
        warn = True
    if block_entries and abs_z is not None and abs_z > cfg.block_abs_z:
        issues.append("abs_z_block")
        block = True

    ok = fair.model_status == MODEL_STATUS_READY and not issues
    return ModelSanityResult(
        ok=ok and not warn,
        warn=warn,
        block_entry=block,
        issues=tuple(issues),
        abs_z=abs_z,
        computed_z=computed_z,
    )
