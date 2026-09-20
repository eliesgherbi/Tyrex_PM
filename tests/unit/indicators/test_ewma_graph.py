"""IndicatorGraph EWMA params and host ingest (F2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.facts import ids as F
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity
from tyrex_pm.indicators.ewma_volatility import (
    SIGMA_UNITS_PER_SQRT_SECOND,
    EwmaVolatilityEstimator,
    SigmaConfig,
    VolatilitySnapshot,
)
from tyrex_pm.indicators.graph import FeatureBundle, IndicatorGraph, sigma_config_from_spec
from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime
from tyrex_pm.strategies.registry import get_strategy_plugin, plugin_indicator_specs
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.driver import create_z_gap_driver
from tyrex_pm.strategies.z_gap.plugin import ewma_vol_spec_from_volatility


def _ewma_spec(**overrides: str) -> IndicatorSpec:
    params = {
        "half_life_s": "30.0",
        "min_samples_s": "20.0",
        "jump_threshold_sigma": "4.0",
        "sample_interval_s": "1.0",
        "tau_floor_s": "1.0",
        "sigma_floor": "0.0001",
        **overrides,
    }
    return IndicatorSpec(
        name="ewma_vol",
        source=F.BINANCE_SPOT_TRADES,
        params=tuple(params.items()),
    )


def test_ewma_graph_rejects_missing_params() -> None:
    spec = IndicatorSpec(name="ewma_vol", source=F.BINANCE_SPOT_TRADES)
    with pytest.raises(ValueError, match="missing required params"):
        IndicatorGraph((spec,))


def test_ewma_graph_rejects_unknown_params() -> None:
    spec = _ewma_spec(not_a_field="1")
    with pytest.raises(ValueError, match="unknown params"):
        sigma_config_from_spec(spec)


def test_indicator_graph_rejects_unknown_name() -> None:
    spec = IndicatorSpec(name="not_a_producer", source=F.BINANCE_SPOT_TRADES)
    with pytest.raises(ValueError, match="unknown indicator name"):
        IndicatorGraph((spec,))


def test_ewma_graph_matches_direct_estimator() -> None:
    spec = _ewma_spec(half_life_s="7.5")
    cfg = sigma_config_from_spec(spec)
    assert cfg.half_life_s == 7.5
    assert cfg.half_life_s != SigmaConfig().half_life_s
    graph = IndicatorGraph((spec,))
    est = EwmaVolatilityEstimator(cfg)
    start = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
    prices = [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("101.5")]
    for i, price in enumerate(prices):
        ts = start + timedelta(seconds=i)
        graph.on_trade(source=F.BINANCE_SPOT_TRADES, price=price, ts_event=ts)
        est.update(price, ts)
    bundled = graph.snapshot().get(spec.instance_id)
    assert isinstance(bundled, VolatilitySnapshot)
    direct = est.snapshot()
    assert bundled.ready == direct.ready
    assert bundled.sample_count == direct.sample_count
    if direct.sigma is None:
        assert bundled.sigma is None
    else:
        assert bundled.sigma == pytest.approx(direct.sigma)


def test_host_ingest_binance_updates_graph_not_driver() -> None:
    runtime = MarketSessionRuntime.create(strategy_kind="z_gap", strategy_config=ZGapConfig())
    assert not hasattr(runtime.driver, "ingest_volatility")
    spec = plugin_indicator_specs(get_strategy_plugin("z_gap"), runtime.driver.config)[0]
    start = datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)
    for i, price in enumerate((Decimal("64000"), Decimal("64010"), Decimal("64020"))):
        ts = start + timedelta(seconds=i)
        runtime.ingest_binance(
            PriceTickView(
                value=price,
                source_ts=ts,
                receive_wall_raw_utc=ts,
                receive_wall_corrected_utc=ts,
                identity=TradingReferenceIdentity.BINANCE_SPOT,
            )
        )
    bundled = runtime.indicator_graph.snapshot().get(spec.instance_id)
    assert isinstance(bundled, VolatilitySnapshot)
    assert bundled.sample_count >= 1


def test_ask70_runtime_graph_is_empty_and_ingest_is_harmless() -> None:
    plugin = get_strategy_plugin("ask70")
    config = plugin.load_config(
        {
            "entry_ask_threshold": "0.70",
            "leg_preference": "first_hit",
            "max_price_pad": "0.08",
            "tau_min_s": 10.0,
            "tau_max_s": 290.0,
            "max_clock_uncertainty_ms": 750.0,
        },
        "test",
    )
    runtime = MarketSessionRuntime.create(strategy_kind="ask70", strategy_config=config)
    assert runtime.indicator_graph.specs == ()
    ts = datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc)
    runtime.ingest_binance(
        PriceTickView(
            value=Decimal("1"),
            source_ts=ts,
            receive_wall_raw_utc=ts,
            identity=TradingReferenceIdentity.BINANCE_SPOT,
        )
    )
    assert runtime.indicator_graph.snapshot().values == {}


def test_z_gap_spec_builder_uses_config_sigma_floor() -> None:
    cfg = ZGapConfig()
    spec = ewma_vol_spec_from_volatility(cfg.volatility)
    assert spec.param_map["sigma_floor"] == str(cfg.volatility.sigma_floor)


def test_z_gap_driver_maps_bundle_ewma_snapshot() -> None:
    clock = FakeClock(_wall=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc))
    driver = create_z_gap_driver(config=ZGapConfig(), clock=clock)
    spec = ewma_vol_spec_from_volatility(driver.config.volatility)
    snap = VolatilitySnapshot(
        sigma=0.0123,
        sigma_units=SIGMA_UNITS_PER_SQRT_SECOND,
        ready=True,
        sample_count=40,
        effective_samples_s=30.0,
        last_update_ts=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc),
        jump_guard_tripped=False,
        reject_reason=None,
    )
    assert driver._volatility(FeatureBundle(values={spec.instance_id: snap})) is snap
    empty = driver._volatility(FeatureBundle())
    assert empty.ready is False
    assert empty.sigma is None
