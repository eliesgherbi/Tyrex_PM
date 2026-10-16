"""Thin experiment helpers for the strategy notebooks.

Notebooks configure and visualize; they never reimplement strategy logic. Every run
goes through ``research.strategy_replay.run_window`` with the real plugin driver.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from tyrex_pm.data_warehouse.query.reader import Reader
from tyrex_pm.research.replay.config import Scenario, load_scenario
from tyrex_pm.research.strategy_replay import (
    WindowCatalog,
    WindowRef,
    load_window,
    run_window,
    write_result,
)
from tyrex_pm.research.strategy_replay.cache import WindowCache, cached_catalog
from tyrex_pm.research.strategy_replay.loader import WindowData
from tyrex_pm.research.strategy_replay.results import decision_digest
from tyrex_pm.strategies.settlement_value.schema import settlement_value_config_from_parameters

REPO = Path(__file__).resolve().parents[4]
DEFAULT_WAREHOUSE = REPO / "var" / "warehouse_btc5m"
DEFAULT_COLLECTION = "380e44875c8e483aa4f76e17cab06c02"
SCENARIOS = REPO / "config" / "research" / "scenarios"
RESULTS = REPO / "var" / "research" / "strategy_runs"
CACHE = REPO / "var" / "research" / "strategy_replay" / "cache"

# Research-only defaults layered under every experiment: replayed K is sealed without
# attestation (PROVISIONAL), so research runs must accept it explicitly.
RESEARCH_BASE_PARAMETERS: dict[str, Any] = {
    "quality": {"accepted_k_qualities": ["CONFIRMED_CANONICAL", "PROVISIONAL"]},
}


def merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: dict(v) if isinstance(v, Mapping) else v for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


@dataclass
class Lab:
    """One collection, its catalog and an in-process cache of loaded windows."""

    warehouse: Path = DEFAULT_WAREHOUSE
    collection: str = DEFAULT_COLLECTION
    warmup_s: float = 300.0
    cache_dir: Path | None = CACHE
    _catalog: WindowCatalog | None = None
    _windows: dict[str, WindowData] = field(default_factory=dict)
    _cache: WindowCache | None = None

    @property
    def catalog(self) -> WindowCatalog:
        if self._catalog is None:
            reader = Reader(self.warehouse, self.collection)
            self._catalog = (
                WindowCatalog(reader)
                if self.cache_dir is None
                else cached_catalog(reader, self.cache_dir)
            )
        return self._catalog

    @property
    def cache(self) -> WindowCache | None:
        if self._cache is None and self.cache_dir is not None:
            self._cache = WindowCache(self.cache_dir)
        return self._cache

    def recorded_intervals(self, *, max_gap_s: float = 10.0) -> list[tuple[int, int]]:
        """Contiguous received-time spans covered by committed builds.

        Builds are merged when separated by at most ``max_gap_s``; a longer silence (a
        stopped collector, a separate session) splits the recording. This is coverage
        of *any* data, not per-feed quality: feed gaps inside a span stay visible in
        replay as invalid books, stale tapes and recorded limitations.
        """
        spans = sorted(
            (b["first_received_at"], b["last_received_at"]) for b in self.catalog.reader.manifests
        )
        merged: list[list[int]] = []
        tolerance = int(max_gap_s * 1e9)
        for left, right in spans:
            if merged and left <= merged[-1][1] + tolerance:
                merged[-1][1] = max(merged[-1][1], right)
            else:
                merged.append([left, right])
        return [(a, b) for a, b in merged]

    def windows(self, *, recorded_only: bool = True, after_s: float = 30.0) -> list[WindowRef]:
        """Windows whose [start - warm-up, end + after] lies inside one recorded span."""
        refs = self.catalog.windows()
        if not recorded_only:
            return refs
        spans = self.recorded_intervals()
        warm, after = int(self.warmup_s * 1e9), int(after_s * 1e9)
        return [
            r
            for r in refs
            if any(a <= r.window_start_ns - warm and r.window_end_ns + after <= b for a, b in spans)
        ]

    def window(self, market_id: str) -> WindowData:
        if market_id not in self._windows:
            if self.cache is None:
                window = load_window(self.catalog, market_id, warmup_s=self.warmup_s)
            else:
                window = self.cache.load(self.catalog, market_id, warmup_s=self.warmup_s)
            self._windows[market_id] = window
        return self._windows[market_id]

    def scenario(self, name: str, overrides: Mapping[str, Any] | None = None) -> Scenario:
        """A named preset, optionally with field overrides (re-validated, renamed)."""
        base = load_scenario(SCENARIOS / f"{name}.yaml")
        if not overrides:
            return base
        label = ",".join(f"{k}={v}" for k, v in sorted(overrides.items()))
        scenario = replace(base, name=f"{base.name}[{label}]", **dict(overrides))
        scenario.validate()
        return scenario

    def run(
        self,
        market_id: str,
        *,
        parameters: Mapping[str, Any] | None = None,
        scenario: str = "favorable",
        scenario_overrides: Mapping[str, Any] | None = None,
        save: bool = False,
        execution_policy: Mapping[str, Any] | None = None,
    ) -> dict:
        params = merge(RESEARCH_BASE_PARAMETERS, parameters or {})
        config = settlement_value_config_from_parameters(params, file="notebook")
        window = self.window(market_id)
        kwargs = {}
        if execution_policy:
            kwargs["execution_policy"] = replay_execution_policy(execution_policy)
        result = run_window(
            window,
            strategy_config=config,
            scenario=self.scenario(scenario, scenario_overrides),
            clock_observations=self.catalog.clock_observations_for(
                window.range_start_ns, window.range_end_ns
            ),
            **kwargs,
        )
        result["parameters"] = params
        result["decision_digest"] = decision_digest(result)
        if save:
            result["saved_to"] = str(write_result(result, RESULTS / self.collection))
        return result


def replay_execution_policy(raw: Mapping[str, Any]):
    """``ReplayExecutionPolicy`` from plain values (a ``protection`` mapping is parsed
    with the live run-config parser, so replay and live read the same SL/TP block)."""
    from decimal import Decimal

    from tyrex_pm.protection.spec import protection_spec_from_mapping
    from tyrex_pm.research.strategy_replay.run import ReplayExecutionPolicy

    values = dict(raw)
    if values.get("protection") is not None:
        values["protection"] = protection_spec_from_mapping(dict(values["protection"]))
    for key in ("fee_reserve_rate", "maximum_total_debit", "minimum_entry_depth_shares",
                "minimum_exit_price", "sell_size_increment"):
        if values.get(key) is not None:
            values[key] = Decimal(str(values[key]))
    return ReplayExecutionPolicy(**values)


def grid(axes: Mapping[str, Iterable[Any]]) -> list[dict[str, Any]]:
    """Cartesian product of dotted parameter paths, e.g. {"entry.min_edge_per_share": [...]}."""
    names = list(axes)
    out = []
    for values in itertools.product(*(list(axes[n]) for n in names)):
        params: dict[str, Any] = {}
        for name, value in zip(names, values):
            section, key = name.split(".", 1)
            params.setdefault(section, {})[key] = value
        out.append(params)
    return out


def label(params: Mapping[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, default=str)
