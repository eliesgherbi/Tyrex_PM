"""Run only the IndicatorSpecs a strategy subscribed to."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any

from tyrex_pm.core.clock import Clock, SystemClock
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.indicators.depth import DepthSnapshot, DepthStore
from tyrex_pm.indicators.ewma_volatility import (
    PER_SAMPLE_V0,
    SIGMA_UNITS_PER_SQRT_SECOND,
    EwmaVolatilityEstimator,
    SigmaConfig,
    VolatilitySnapshot,
)
from tyrex_pm.indicators.imbalance import imbalance, microprice_gap_bps
from tyrex_pm.indicators.ofi import RollingOfi
from tyrex_pm.indicators.realized_vol import RollingRealizedVol
from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.indicators.spot_path import RollingSpotPath, SpotPathSnapshot, empty_spot_path
from tyrex_pm.indicators.trade_imbalance import RollingTradeImbalance, TradePrint

_KNOWN_NAMES = frozenset(
    {
        "ofi",
        "trade_imbalance",
        "realized_vol",
        "ewma_vol",
        "imbalance",
        "microprice_gap",
        "momentum",
        "basis",
        "spot_path",
    }
)

_EWMA_PARAM_KEYS = (
    "half_life_s",
    "min_samples_s",
    "jump_threshold_sigma",
    "sample_interval_s",
    "tau_floor_s",
    "sigma_floor",
)
# Optional keys, absent from specs at their original behaviour so existing indicator
# instance ids are unchanged.
_EWMA_OPTIONAL_KEYS = ("variance_time_policy",)


@dataclass
class FeatureBundle:
    """Latest values keyed by IndicatorSpec.instance_id (and horizon/level suffix)."""

    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def empty_volatility_snapshot(*, reject_reason: str = "warming_up") -> VolatilitySnapshot:
    return VolatilitySnapshot(
        sigma=None,
        sigma_units=SIGMA_UNITS_PER_SQRT_SECOND,
        ready=False,
        sample_count=0,
        effective_samples_s=0.0,
        last_update_ts=None,
        jump_guard_tripped=False,
        reject_reason=reject_reason,
    )


def sigma_config_from_spec(spec: IndicatorSpec) -> SigmaConfig:
    if spec.name != "ewma_vol":
        raise ValueError(f"sigma_config_from_spec requires ewma_vol, got {spec.name!r}")
    mapping = spec.param_map
    missing = [key for key in _EWMA_PARAM_KEYS if key not in mapping]
    extra = sorted(set(mapping) - set(_EWMA_PARAM_KEYS) - set(_EWMA_OPTIONAL_KEYS))
    if missing:
        raise ValueError(f"ewma_vol missing required params: {missing}")
    if extra:
        raise ValueError(f"ewma_vol unknown params: {extra}")
    return SigmaConfig(
        half_life_s=float(mapping["half_life_s"]),
        min_samples_s=float(mapping["min_samples_s"]),
        jump_threshold_sigma=float(mapping["jump_threshold_sigma"]),
        sample_interval_s=float(mapping["sample_interval_s"]),
        tau_floor_s=float(mapping["tau_floor_s"]),
        sigma_floor=float(mapping["sigma_floor"]),
        variance_time_policy=mapping.get("variance_time_policy", PER_SAMPLE_V0),
    )


class IndicatorGraph:
    """Stateful producers for a frozen set of IndicatorSpecs."""

    def __init__(
        self,
        specs: tuple[IndicatorSpec, ...],
        *,
        max_trade_age_ms: float = 2000.0,
        clock: Clock | None = None,
    ) -> None:
        unknown = sorted({spec.name for spec in specs} - _KNOWN_NAMES)
        if unknown:
            raise ValueError(f"unknown indicator name(s): {unknown}")
        self.specs = specs
        # Freshness clock: host monotonic time live, virtual recorded time in replay.
        self._clock: Clock = clock or SystemClock()
        self.depth = DepthStore()
        self._bundle = FeatureBundle()
        self._ofi: dict[str, RollingOfi] = {}
        self._ti: dict[str, RollingTradeImbalance] = {}
        self._rv: dict[str, RollingRealizedVol] = {}
        self._ewma: dict[str, EwmaVolatilityEstimator] = {}
        self._paths: dict[str, RollingSpotPath] = {}
        self._mids: dict[str, Decimal] = {}
        self._source_generation: dict[str, int] = {}
        self._last_trade_mono_ns: dict[str, int] = {}
        self.max_trade_age_ms: float = float(max_trade_age_ms)
        for spec in specs:
            if spec.name == "ofi":
                for horizon in spec.horizons_ms or (1000,):
                    self._ofi[f"{spec.instance_id}|{horizon}"] = RollingOfi(
                        horizon=timedelta(milliseconds=horizon)
                    )
            elif spec.name == "trade_imbalance":
                for horizon in spec.horizons_ms or (1000,):
                    self._ti[f"{spec.instance_id}|{horizon}"] = RollingTradeImbalance(
                        horizon=timedelta(milliseconds=horizon)
                    )
            elif spec.name == "realized_vol":
                for horizon in spec.horizons_ms or (10000,):
                    self._rv[f"{spec.instance_id}|{horizon}"] = RollingRealizedVol(
                        horizon=timedelta(milliseconds=horizon)
                    )
            elif spec.name == "ewma_vol":
                self._ewma[spec.instance_id] = EwmaVolatilityEstimator(
                    sigma_config_from_spec(spec)
                )
                self._bundle.values[spec.instance_id] = empty_volatility_snapshot()
            elif spec.name == "spot_path":
                self._paths[spec.instance_id] = _spot_path_from_spec(spec)
                self._bundle.values[spec.instance_id] = empty_spot_path()

    def explain(self) -> dict[str, dict]:
        """Decision-time copy of calculated indicator state (no recomputation).

        Per ``ewma_vol`` instance: the estimator's ``explain()`` plus the age of the last
        trade on its source tape (the freshness input the host gate uses).
        """
        now_mono = self._clock.monotonic_ns()
        out: dict[str, dict] = {}
        for spec in self.specs:
            est = self._ewma.get(spec.instance_id)
            if est is None:
                continue
            last = self._last_trade_mono_ns.get(spec.source)
            out[spec.instance_id] = {
                **est.explain(),
                "source": spec.source,
                "tape_age_ms": None if last is None else round((now_mono - last) / 1e6, 3),
                "max_trade_age_ms": self.max_trade_age_ms,
            }
        return out

    def snapshot(self) -> FeatureBundle:
        now_mono = self._clock.monotonic_ns()
        values = dict(self._bundle.values)
        for spec in self.specs:
            if spec.name not in ("ewma_vol", "spot_path"):
                continue
            snap = values.get(spec.instance_id)
            if not isinstance(snap, (VolatilitySnapshot, SpotPathSnapshot)) or not snap.ready:
                continue
            last = self._last_trade_mono_ns.get(spec.source)
            if last is None or (now_mono - last) / 1e6 > self.max_trade_age_ms:
                values[spec.instance_id] = replace(
                    snap, ready=False, reject_reason="stale_tape"
                )
        return FeatureBundle(values=values)

    def invalidate_source(self, source: str) -> None:
        current = self._source_generation.get(source, 0)
        self._source_generation[source] = current + 1
        self._last_trade_mono_ns.pop(source, None)
        for spec in self.specs:
            if spec.source != source or spec.name != "ewma_vol":
                continue
            self._ewma[spec.instance_id] = EwmaVolatilityEstimator(
                sigma_config_from_spec(spec)
            )
            self._bundle.values[spec.instance_id] = empty_volatility_snapshot(
                reject_reason="source_invalidated"
            )
        for spec in self.specs:
            if spec.source == source and spec.name == "spot_path":
                # A reconnect breaks the sampled path; history restarts from the new tape.
                self._paths[spec.instance_id] = _spot_path_from_spec(spec)
                self._bundle.values[spec.instance_id] = empty_spot_path(
                    reject_reason="source_invalidated"
                )

    def on_depth(
        self,
        snapshot: DepthSnapshot,
        *,
        source: str,
        already_applied: bool = False,
    ) -> FeatureBundle:
        if already_applied:
            previous_mid = self._mids.get(source)
        else:
            previous_mid = None
            prev = self.depth.capture(snapshot.instrument_id)
            if prev is not None:
                previous_mid = prev.mid
            self.depth.apply(snapshot)
        mid = snapshot.mid
        if mid is not None:
            self._mids[source] = mid
            if source.endswith(".l2"):
                self._mids[source[: -len(".l2")] + ".mid"] = mid
        for spec in self.specs:
            if spec.source not in {source, _mid_fact(source)} and source not in spec.all_sources:
                continue
            if spec.name == "imbalance":
                levels = spec.levels or (1,)
                for level in levels:
                    self._bundle.values[f"{spec.instance_id}|{level}"] = imbalance(
                        snapshot, levels=level
                    )
            elif spec.name == "microprice_gap":
                self._bundle.values[spec.instance_id] = microprice_gap_bps(snapshot)
            elif spec.name == "ofi":
                for horizon in spec.horizons_ms or (1000,):
                    key = f"{spec.instance_id}|{horizon}"
                    self._bundle.values[key] = self._ofi[key].update(snapshot)
            elif spec.name == "momentum" and mid is not None and previous_mid is not None:
                self._update_momentum(spec, mid=mid, previous_mid=previous_mid)
            elif spec.name == "basis":
                self._update_basis(spec)
        return self.snapshot()

    def on_trade(
        self,
        *,
        source: str,
        price: Decimal,
        ts_event,
        quantity: Decimal | None = None,
        aggressor: str | None = None,
        instrument_id: InstrumentId | None = None,
        generation: int | None = None,
    ) -> FeatureBundle:
        del instrument_id
        if generation is not None:
            current = self._source_generation.get(source)
            if current is None:
                self._source_generation[source] = generation
            elif generation < current:
                return self.snapshot()
            elif generation > current:
                self.invalidate_source(source)
                self._source_generation[source] = generation
        self._last_trade_mono_ns[source] = self._clock.monotonic_ns()
        for spec in self.specs:
            if spec.source != source:
                continue
            if spec.name == "ewma_vol":
                est = self._ewma[spec.instance_id]
                snap = est.update(price, ts_event)
                self._bundle.values[spec.instance_id] = snap
            elif spec.name == "spot_path":
                self._bundle.values[spec.instance_id] = self._paths[spec.instance_id].update(
                    price, ts_event
                )
            elif spec.name == "realized_vol":
                for horizon in spec.horizons_ms or (10000,):
                    key = f"{spec.instance_id}|{horizon}"
                    self._bundle.values[key] = self._rv[key].update(
                        price=price, ts_event=ts_event
                    )
            elif spec.name == "trade_imbalance" and quantity is not None and aggressor in {
                "buy",
                "sell",
            }:
                print_ = TradePrint(
                    ts_event=ts_event,
                    price=price,
                    quantity=quantity,
                    aggressor=aggressor,  # type: ignore[arg-type]
                )
                for horizon in spec.horizons_ms or (1000,):
                    key = f"{spec.instance_id}|{horizon}"
                    self._bundle.values[key] = self._ti[key].ingest(print_)
        return self.snapshot()

    def _update_momentum(
        self, spec: IndicatorSpec, *, mid: Decimal, previous_mid: Decimal
    ) -> None:
        del previous_mid
        self._bundle.values[f"{spec.instance_id}|last_mid"] = mid

    def _update_basis(self, spec: IndicatorSpec) -> None:
        spot = self._mids.get(spec.source)
        perp_source = spec.extra_sources[0] if spec.extra_sources else None
        perp = None if perp_source is None else self._mids.get(perp_source)
        if spot is None or perp is None or spot <= 0 or perp <= 0:
            self._bundle.values[spec.instance_id] = None
            return
        from math import log

        self._bundle.values[spec.instance_id] = Decimal("10000") * Decimal(
            str(log(float(perp / spot)))
        )


def _spot_path_from_spec(spec: IndicatorSpec) -> RollingSpotPath:
    mapping = spec.param_map
    extra = sorted(set(mapping) - {"window_s", "sample_interval_s"})
    if extra or "window_s" not in mapping:
        raise ValueError(f"spot_path requires window_s (optional sample_interval_s); got {extra}")
    return RollingSpotPath(
        window_s=float(mapping["window_s"]),
        sample_interval_s=float(mapping.get("sample_interval_s", "1")),
    )


def _mid_fact(source: str) -> str:
    if source.endswith(".l2"):
        return source[: -len(".l2")] + ".mid"
    return source
