"""Run only the IndicatorSpecs a strategy subscribed to."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.indicators.depth import DepthSnapshot, DepthStore
from tyrex_pm.indicators.ewma_volatility import EwmaVolatilityEstimator, SigmaConfig
from tyrex_pm.indicators.imbalance import imbalance, microprice_gap_bps
from tyrex_pm.indicators.ofi import RollingOfi
from tyrex_pm.indicators.realized_vol import RollingRealizedVol
from tyrex_pm.indicators.spec import IndicatorSpec
from tyrex_pm.indicators.trade_imbalance import RollingTradeImbalance, TradePrint


@dataclass
class FeatureBundle:
    """Latest values keyed by IndicatorSpec.instance_id (and horizon/level suffix)."""

    values: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class IndicatorGraph:
    """Stateful producers for a frozen set of IndicatorSpecs."""

    def __init__(self, specs: tuple[IndicatorSpec, ...]) -> None:
        self.specs = specs
        self.depth = DepthStore()
        self._bundle = FeatureBundle()
        self._ofi: dict[str, RollingOfi] = {}
        self._ti: dict[str, RollingTradeImbalance] = {}
        self._rv: dict[str, RollingRealizedVol] = {}
        self._ewma: dict[str, EwmaVolatilityEstimator] = {}
        self._mids: dict[str, Decimal] = {}
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
                self._ewma[spec.instance_id] = EwmaVolatilityEstimator(SigmaConfig())

    def snapshot(self) -> FeatureBundle:
        return FeatureBundle(values=dict(self._bundle.values))

    def on_depth(self, snapshot: DepthSnapshot, *, source: str) -> FeatureBundle:
        previous_mid = None
        prev = self.depth.capture(snapshot.instrument_id)
        if prev is not None:
            previous_mid = prev.mid
        self.depth.apply(snapshot)
        mid = snapshot.mid
        if mid is not None:
            self._mids[source] = mid
            # Treat L2 source as also producing the corresponding .mid fact.
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
    ) -> FeatureBundle:
        del instrument_id
        for spec in self.specs:
            if spec.source != source:
                continue
            if spec.name == "ewma_vol":
                est = self._ewma[spec.instance_id]
                est.update(price, ts_event)
                snap = est.snapshot()
                self._bundle.values[spec.instance_id] = {
                    "sigma": snap.sigma,
                    "ready": snap.ready,
                }
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
        # Latest mid is stored; horizon momentum is log(S_t / S_{t-h}) approximated
        # by the caller supplying successive mids into realized path. Store last mid.
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


def _mid_fact(source: str) -> str:
    if source.endswith(".l2"):
        return source[: -len(".l2")] + ".mid"
    return source
