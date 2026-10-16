"""Prepared-window cache identity, limit buffers and scenario overrides."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.research.strategy_replay import cache as cache_mod
from tyrex_pm.research.strategy_replay.feed import PointClock
from tyrex_pm.strategies.settlement_value.economics import buffered_limit

NO_FEE = FeeCurveParams(fee_rate=Decimal("0"), exponent=Decimal("1"))
FEE = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))


def test_buffered_limit_bounds() -> None:
    kwargs = dict(tick=Decimal("0.01"), max_entry_price=Decimal("0.97"), fee_curve=NO_FEE)
    assert buffered_limit(Decimal("0.40"), p_conservative=0.6, buffer_ticks=0, **kwargs) == (
        Decimal("0.40"),
        "no_buffer",
    )
    assert buffered_limit(Decimal("0.40"), p_conservative=0.6, buffer_ticks=2, **kwargs) == (
        Decimal("0.42"),
        "buffer_full",
    )
    # Never to a price whose conservative value is negative (0.6 * net - p < 0 above 0.6).
    limit, bound = buffered_limit(Decimal("0.59"), p_conservative=0.6, buffer_ticks=5, **kwargs)
    assert limit == Decimal("0.60") and bound == "nonnegative_value"
    limit, bound = buffered_limit(
        Decimal("0.96"), p_conservative=0.999, buffer_ticks=5, tick=Decimal("0.01"),
        max_entry_price=Decimal("0.97"), fee_curve=FEE,
    )
    assert limit == Decimal("0.97") and bound == "max_entry_price"
    assert buffered_limit(
        Decimal("0.40"), p_conservative=0.6, buffer_ticks=1, tick=None,
        max_entry_price=Decimal("0.97"), fee_curve=NO_FEE,
    ) == (Decimal("0.40"), "buffer_skipped_tick_unknown")


def test_point_clock_can_be_set_back_for_exact_store_apply_times() -> None:
    clock = PointClock(100)
    clock.at(50)
    assert clock.monotonic_ns() == 50
    clock.at(2_000_000_000)
    assert clock.now_utc().timestamp() == pytest.approx(2.0)


def _fake_catalog(tmp_path: Path, *, sha: str, resolutions=()):
    root = tmp_path / "wh"
    (root / "collections" / "c").mkdir(parents=True, exist_ok=True)
    (root / "collections" / "c" / "collection.json").write_text(
        '{"profile_hash": "p", "schema_version": "1", "parser_version": "1"}', encoding="utf-8"
    )
    reader = SimpleNamespace(
        root=root,
        collection="c",
        manifests=[
            {
                "build_id": "b1",
                "first_received_at": 0,
                "last_received_at": 10**18 * 3,
                "files": [{"path": "x.parquet", "sha256": sha}],
            }
        ],
    )
    gamma = {"source_id": "gamma", "window_start": 10**18, "window_end": 10**18 + 300 * 10**9}
    return SimpleNamespace(
        reader=reader,
        gamma_rows=lambda market_id: [gamma],
        metadata={"m": [{"record_id": "r1", "received_at": 1}]},
        instruments={"m": []},
        resolutions={"m": list(resolutions)},
    )


def test_cache_key_changes_with_source_checksums_and_late_resolutions(tmp_path: Path) -> None:
    def key_of(catalog, warmup_s=300):
        return cache_mod.window_cache_key(catalog, "m", warmup_s=warmup_s, after_s=30)

    key = key_of(_fake_catalog(tmp_path, sha="a"))
    same = key_of(_fake_catalog(tmp_path, sha="a"))
    assert key == same
    assert key != cache_mod.window_cache_key(
        _fake_catalog(tmp_path, sha="b"), "m", warmup_s=300, after_s=30
    )
    assert key != cache_mod.window_cache_key(
        _fake_catalog(tmp_path, sha="a", resolutions=[{"record_id": "z", "received_at": 9}]),
        "m",
        warmup_s=300,
        after_s=30,
    )
    assert key != cache_mod.window_cache_key(
        _fake_catalog(tmp_path, sha="a"), "m", warmup_s=120, after_s=30
    )


def test_cache_rebuilds_on_key_mismatch_and_corruption(tmp_path: Path, monkeypatch) -> None:
    built: list[str] = []

    def fake_load(catalog, market_id, *, warmup_s, after_s):
        built.append(market_id)
        return cache_mod.WindowData.__new__(cache_mod.WindowData)

    monkeypatch.setattr(cache_mod, "load_window", fake_load)
    catalog = _fake_catalog(tmp_path, sha="a")
    store = cache_mod.WindowCache(tmp_path / "cache")
    store.load(catalog, "m")
    store.load(catalog, "m")
    assert (store.misses, store.hits) == (1, 1) and built == ["m"]
    store.path(catalog, "m").write_bytes(b"not a cache entry")
    store.load(catalog, "m")
    assert store.misses == 2
    changed = _fake_catalog(tmp_path, sha="b")
    store.load(changed, "m")
    assert store.misses == 3


def test_scenario_overrides_are_validated_and_named() -> None:
    from tyrex_pm.research.strategy_lab.experiments import Lab

    lab = Lab(cache_dir=None)
    scenario = lab.scenario("friction", {"order_delay_ms": 50, "adverse_price_ticks": 0})
    assert scenario.order_delay_ms == 50 and "order_delay_ms=50" in scenario.name
    with pytest.raises(ValueError):
        lab.scenario("friction", {"participation": "0"})
