"""Spot path integration and the twap_proxy_diffusion backend."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.indicators.spot_path import RollingSpotPath, path_mean
from tyrex_pm.strategies.settlement_value.config import SettlementModelConfig
from tyrex_pm.strategies.settlement_value.twap_proxy import twap_moments, twap_proxy_probability

T0 = datetime(2026, 10, 6, 17, 50, tzinfo=timezone.utc)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def path_from(prices: dict[int, str], window_s: float = 180):
    path = RollingSpotPath(window_s=window_s)
    snap = None
    for second, price in sorted(prices.items()):
        snap = path.update(Decimal(price), at(second + 0.5))
    return snap


def test_step_path_mean_uses_values_only_after_observable() -> None:
    snap = path_from({0: "100", 10: "110"})
    # Bucket 0 applies from t=1 to t=11, bucket 10 from t=11 onwards.
    mean, coverage = path_mean(snap, at(1), at(21))
    assert mean == Decimal("105") and coverage == pytest.approx(1.0)
    mean, coverage = path_mean(snap, at(0), at(2))
    assert coverage == pytest.approx(0.5)  # nothing observable during [0, 1)


def test_out_of_order_print_never_rewrites_history() -> None:
    path = RollingSpotPath(window_s=60)
    path.update(Decimal("100"), at(10))
    snap = path.update(Decimal("999"), at(5))
    assert [p for _, p in snap.samples] == [Decimal("100")]


def test_twap_moments_match_the_averaging_formulas() -> None:
    m, v = twap_moments(spot=100.0, basis=-1.0, observed_mean=None, sigma_d=2.0, h=120, window_s=60)
    assert m == pytest.approx(99.0) and v == pytest.approx(4.0 * (120 - 40))
    m, v = twap_moments(spot=100.0, basis=0.0, observed_mean=90.0, sigma_d=2.0, h=30, window_s=60)
    assert m == pytest.approx((90 * 30 + 30 * 100) / 60)
    assert v == pytest.approx(4.0 * 30**3 / (3 * 60**2))
    m, v = twap_moments(spot=100.0, basis=0.0, observed_mean=90.0, sigma_d=2.0, h=0, window_s=60)
    assert m == pytest.approx(90.0) and v == 0.0


def model(**kw) -> SettlementModelConfig:
    return SettlementModelConfig(backend="twap_proxy_diffusion", **kw)


def test_basis_is_report_minus_binance_mean_over_the_report_window() -> None:
    snap = path_from({s: "100" for s in range(0, 200)})
    prob, diag = twap_proxy_probability(
        spot=Decimal("100"), k=Decimal("98"), sigma=1e-4, now=at(150), window_end=at(300),
        path=snap, twap_obs=Decimal("98"), twap_ts=at(149), model=model(),
    )
    assert prob.ready
    assert diag["basis_usd"] == pytest.approx(-2.0)
    # m = S + b = 98 = K  ->  p = 0.5 before the averaging window.
    assert prob.p_up_point == pytest.approx(0.5)
    assert prob.p_up_low <= 0.5 <= prob.p_up_high


def test_inside_window_requires_observed_history_and_collapses_at_close() -> None:
    # A reconnect resets the path inside the averaging window: history is incomplete.
    reset = path_from({s: "100" for s in range(285, 295)})
    prob, _ = twap_proxy_probability(
        spot=Decimal("100"), k=Decimal("99"), sigma=1e-4, now=at(295), window_end=at(300),
        path=reset, twap_obs=Decimal("100"), twap_ts=at(294), model=model(),
    )
    assert not prob.ready
    assert prob.reason in {"basis_window_incomplete", "observed_average_incomplete"}
    full = path_from({s: "101" for s in range(0, 300)})
    prob, _ = twap_proxy_probability(
        spot=Decimal("101"), k=Decimal("100"), sigma=1e-4, now=at(299.9), window_end=at(300),
        path=full, twap_obs=Decimal("101"), twap_ts=at(299), model=model(),
    )
    assert prob.ready and prob.p_up_point > 0.999


def test_missing_inputs_are_not_ready_never_zero() -> None:
    # History starts after the report's window opens: the basis cannot be estimated.
    # (A step path carries its last price forward; staleness is the graph's job.)
    snap = path_from({s: "100" for s in range(140, 150)})
    prob, _ = twap_proxy_probability(
        spot=Decimal("100"), k=Decimal("100"), sigma=1e-4, now=at(150), window_end=at(300),
        path=snap, twap_obs=Decimal("100"), twap_ts=at(149), model=model(),
    )
    assert not prob.ready and prob.reason == "basis_window_incomplete"
    prob, _ = twap_proxy_probability(
        spot=Decimal("100"), k=Decimal("100"), sigma=1e-4, now=at(150), window_end=at(300),
        path=snap, twap_obs=None, twap_ts=None, model=model(),
    )
    assert not prob.ready and prob.reason == "twap_report_missing"
