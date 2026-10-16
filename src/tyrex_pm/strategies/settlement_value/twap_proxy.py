"""``twap_proxy_diffusion``: settlement probability for a TWAP-at-close payoff (PROXY).

Assumed functional (S0 diagnostic, provenance ASSUMED, not attested):

    A_T = (1/L) * integral_{T-L}^{T} C_u du,   Up  iff  A_T >= K,   K = A_start

where C is the official (Chainlink) underlying and L the averaging window. Neither the
constituents C_u nor their weights are recorded, so this backend is a **proxy**:

* Constituents: C_u ~ B_u + b, the raw Binance spot path (sampled step path) plus a
  basis b assumed constant over the window. Binance prints are averaged; the reported
  TWAP is never averaged again.
* Basis: b = A_obs(t') - mean_{[t'-L, t']} B, i.e. the latest reported TWAP minus the
  Binance mean over that report's own window (like with like). This absorbs the
  USD/USDT difference and any constant feed offset, not their changes.
* Dynamics: arithmetic diffusion with dollar volatility sigma_d = S * sigma (explicit
  approximation from the log-return EWMA sigma), zero drift. With h = T - t:
    h >= L: m = S_t + b,                      v = sigma_d^2 (h - 2L/3)
    h <  L: m = (I_t + h (S_t + b)) / L,      v = sigma_d^2 h^3 / (3 L^2)
  where I_t is the already observed part of the average (Binance path + basis).
* Interval: the same sigma band and reference band as the endpoint backend; a method
  label, not a statistical guarantee.

Equality at v = 0 uses the comparison ``>=`` from the rules text (ASSUMED).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from decimal import Decimal

from tyrex_pm.indicators.binary_fair_value import normal_cdf
from tyrex_pm.indicators.spot_path import SpotPathSnapshot, path_mean
from tyrex_pm.strategies.settlement_value.config import SettlementModelConfig
from tyrex_pm.strategies.settlement_value.economics import ProbabilityEstimate

BACKEND = "twap_proxy_diffusion"
MIN_COVERAGE = 0.95


def _p_up(mean: float, variance: float, k: float) -> float:
    if variance <= 0:
        return 1.0 if mean >= k else 0.0
    return normal_cdf((mean - k) / math.sqrt(variance))


def twap_moments(
    *,
    spot: float,
    basis: float,
    observed_mean: float | None,
    sigma_d: float,
    h: float,
    window_s: float,
) -> tuple[float, float]:
    """Mean and variance of the window average given the information at t."""
    if h >= window_s:
        return spot + basis, sigma_d**2 * (h - 2.0 * window_s / 3.0)
    if observed_mean is None:
        raise ValueError("observed part of the average is required inside the window")
    observed_integral = (observed_mean + basis) * (window_s - max(h, 0.0))
    h = max(h, 0.0)
    mean = (observed_integral + h * (spot + basis)) / window_s
    variance = sigma_d**2 * h**3 / (3.0 * window_s**2)
    return mean, variance


def twap_proxy_probability(
    *,
    spot: Decimal | None,
    k: Decimal | None,
    sigma: float | None,
    now: datetime,
    window_end: datetime,
    path: SpotPathSnapshot | None,
    twap_obs: Decimal | None,
    twap_ts: datetime | None,
    model: SettlementModelConfig,
) -> tuple[ProbabilityEstimate, dict]:
    diag: dict = {"backend": BACKEND, "averaging_window_s": model.averaging_window_s}

    def not_ready(reason: str) -> tuple[ProbabilityEstimate, dict]:
        diag["reason"] = reason
        return ProbabilityEstimate(ready=False, reason=reason, backend=BACKEND), diag

    if spot is None or k is None or sigma is None or sigma <= 0:
        return not_ready("missing_spot_k_or_sigma")
    if path is None or not path.ready:
        return not_ready("spot_path_not_ready" if path is None else path.reject_reason or "path")
    if twap_obs is None or twap_ts is None:
        return not_ready("twap_report_missing")
    window = timedelta(seconds=model.averaging_window_s)
    basis_mean, basis_cov = path_mean(path, twap_ts - window, twap_ts)
    diag["basis_window_coverage"] = basis_cov
    if basis_mean is None or basis_cov < MIN_COVERAGE:
        return not_ready("basis_window_incomplete")
    basis = float(twap_obs - basis_mean)
    h = (window_end - now).total_seconds()
    observed_mean = None
    if h < model.averaging_window_s:
        observed_mean_d, coverage = path_mean(path, window_end - window, now)
        diag["observed_window_coverage"] = coverage
        if observed_mean_d is None or coverage < MIN_COVERAGE:
            return not_ready("observed_average_incomplete")
        observed_mean = float(observed_mean_d)
    s, kf = float(spot), float(k)
    shift = model.reference_band_bps / 10_000.0 * kf
    grid = []
    for sig in (sigma * (1.0 - model.sigma_band), sigma, sigma * (1.0 + model.sigma_band)):
        if sig <= 0:
            continue
        for offset in (-shift, 0.0, shift):
            m, v = twap_moments(
                spot=s, basis=basis, observed_mean=observed_mean, sigma_d=s * sig,
                h=h, window_s=model.averaging_window_s,
            )
            grid.append(_p_up(m + offset, v, kf))
    mean, variance = twap_moments(
        spot=s, basis=basis, observed_mean=observed_mean, sigma_d=s * sigma, h=h,
        window_s=model.averaging_window_s,
    )
    point = _p_up(mean, variance, kf)
    diag.update(basis_usd=basis, twap_mean=mean, twap_sd=math.sqrt(max(variance, 0.0)),
                h_s=h, observed_mean=observed_mean)
    z = None if variance <= 0 else (mean - kf) / math.sqrt(variance)
    return (
        ProbabilityEstimate(
            ready=True, reason=None, backend=BACKEND, p_up_point=point,
            p_up_low=min(grid), p_up_high=max(grid), z=z, sigma=sigma, tau_s=h,
            interval_method="sigma_and_reference_band_v1",
        ),
        diag,
    )
