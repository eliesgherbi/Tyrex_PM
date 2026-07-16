"""Composition root for the R3 read-only observe path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.adapters.binance.fixture_source import BinanceFixtureSource
from tyrex_pm.adapters.polymarket.discovery import (
    FixtureMarketDiscovery,
    load_market_from_fixture,
)
from tyrex_pm.adapters.polymarket.fixture_source import PolymarketFixtureSource
from tyrex_pm.core.clock import Clock, FakeClock, SystemClock
from tyrex_pm.core.events import ReferencePriceUpdated
from tyrex_pm.core.ids import CorrelationId, RunId, new_correlation_id, new_run_id
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketRequest
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.indicators.momentum import ShortHorizonMomentum
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import book_quote
from tyrex_pm.market_data.freshness import assess_freshness
from tyrex_pm.market_data.reference_store import ReferenceDataStore
from tyrex_pm.market_data.registry import InstrumentRegistry
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.jsonl import JsonlFactSink
from tyrex_pm.runtime.config import ObserveConfig, SourceMode
from tyrex_pm.signals.directional import DirectionalSignal, build_directional_signal
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ObserveDecision,
    ReferenceMomentumStrategy,
)


@dataclass
class ObserveRunResult:
    run_id: RunId
    correlation_id: CorrelationId
    market: BinaryMarket
    decisions: list[ObserveDecision] = field(default_factory=list)
    signals: list[DirectionalSignal] = field(default_factory=list)
    facts_path: Path | None = None
    fact_count: int = 0


class ObserveHost:
    """Wires clock, dispatcher, stores, indicators, strategy, reporting."""

    def __init__(
        self,
        config: ObserveConfig,
        *,
        clock: Clock | None = None,
        run_id: RunId | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.run_id = run_id or new_run_id()
        self.correlation_id = correlation_id or new_correlation_id()
        self.dispatcher = EventDispatcher()
        self.registry = InstrumentRegistry()
        self.book_store = MarketStateStore()
        self.reference_store = ReferenceDataStore()
        self.momentum = ShortHorizonMomentum(config.momentum)
        self.strategy = ReferenceMomentumStrategy()
        self.sink = JsonlFactSink(config.output_path)
        self.decisions: list[ObserveDecision] = []
        self.signals: list[DirectionalSignal] = []
        self._attached = False

    def _attach(self) -> None:
        if self._attached:
            return
        self.book_store.attach(self.dispatcher)
        self.reference_store.attach(self.dispatcher)
        if self.config.evaluate_on_reference:
            self.dispatcher.subscribe(ReferencePriceUpdated, self._on_reference_evaluate)
        self._attached = True

    def _emit(self, fact_type: str, payload: dict[str, Any], *, causation_id=None, strategy_id=None) -> None:
        fact = make_fact(
            fact_type=fact_type,
            ts=self.clock.now_utc(),
            run_id=self.run_id,
            correlation_id=self.correlation_id,
            payload=payload,
            causation_id=causation_id,
            strategy_id=strategy_id,
        )
        self.sink.append(fact)

    def build_snapshot(self, *, causation_id=None) -> DecisionSnapshot:
        market = self.registry.require_market()
        yes_state = self.book_store.get(market.yes.instrument_id)
        no_state = self.book_store.get(market.no.instrument_id)
        ref_state = self.reference_store.get(self.config.binance_symbol)
        cfg = self.config.freshness
        yes_fresh = assess_freshness(
            clock=self.clock,
            ts_event=yes_state.last_ts_event,
            ts_received=yes_state.last_ts_received,
            initialized=yes_state.initialized,
            threshold_ms=cfg.book_threshold_ms,
            future_tolerance_ms=cfg.future_tolerance_ms,
            basis=cfg.timestamp_basis,
        )
        no_fresh = assess_freshness(
            clock=self.clock,
            ts_event=no_state.last_ts_event,
            ts_received=no_state.last_ts_received,
            initialized=no_state.initialized,
            threshold_ms=cfg.book_threshold_ms,
            future_tolerance_ms=cfg.future_tolerance_ms,
            basis=cfg.timestamp_basis,
        )
        ref_fresh = assess_freshness(
            clock=self.clock,
            ts_event=ref_state.last_ts_event,
            ts_received=ref_state.last_ts_received,
            initialized=ref_state.initialized,
            threshold_ms=cfg.reference_threshold_ms,
            future_tolerance_ms=cfg.future_tolerance_ms,
            basis=cfg.timestamp_basis,
        )
        return DecisionSnapshot(
            market=market,
            yes_book=yes_state.book,
            no_book=no_state.book,
            yes_quote=book_quote(yes_state.book),
            no_quote=book_quote(no_state.book),
            reference=ref_state.snapshot,
            yes_freshness=yes_fresh,
            no_freshness=no_fresh,
            reference_freshness=ref_fresh,
            observed_at=self.clock.now_utc(),
            correlation_id=self.correlation_id,
            causation_id=causation_id,
        )

    def evaluate_once(self, *, causation_id=None) -> ObserveDecision:
        snapshot = self.build_snapshot(causation_id=causation_id)
        self._emit(
            "freshness_assessment",
            {
                "yes": snapshot.yes_freshness.reason_code.value,
                "no": snapshot.no_freshness.reason_code.value,
                "reference": snapshot.reference_freshness.reason_code.value,
                "yes_age_ms": snapshot.yes_freshness.age_ms,
                "no_age_ms": snapshot.no_freshness.age_ms,
                "reference_age_ms": snapshot.reference_freshness.age_ms,
            },
            causation_id=causation_id,
        )

        momentum_value: Decimal | None = None
        momentum_ready = False
        momentum_reason = "NO_REFERENCE"
        if snapshot.reference is not None:
            ind = self.momentum.update(snapshot.reference, source_event_id=causation_id)
            momentum_value = ind.value.get("momentum")
            momentum_ready = bool(ind.value.get("ready"))
            momentum_reason = str(ind.value.get("reason"))
            self._emit(
                "indicator_result",
                {
                    "indicator_id": ind.indicator_id,
                    "momentum": None if momentum_value is None else str(momentum_value),
                    "ready": momentum_ready,
                    "reason": momentum_reason,
                    "yes_mid": None if snapshot.yes_quote.mid is None else str(snapshot.yes_quote.mid),
                    "yes_spread": None
                    if snapshot.yes_quote.spread is None
                    else str(snapshot.yes_quote.spread),
                },
                causation_id=causation_id,
            )

        signal = build_directional_signal(
            snapshot=snapshot,
            momentum=momentum_value,
            momentum_ready=momentum_ready,
            momentum_reason=momentum_reason,
            threshold=self.config.momentum_threshold,
            max_spread=self.config.max_book_spread,
        )
        self.signals.append(signal)
        self._emit(
            "signal",
            {
                "direction": signal.direction.value,
                "reason_code": signal.reason_code,
                "momentum": None if signal.momentum is None else str(signal.momentum),
                "threshold": str(signal.threshold),
                "strength": None if signal.strength is None else str(signal.strength),
                "selected_outcome": None
                if signal.selected_outcome is None
                else signal.selected_outcome.value,
                "evidence": signal.evidence,
            },
            causation_id=signal.causation_id,
            strategy_id=ReferenceMomentumStrategy.STRATEGY_ID,
        )

        decision = self.strategy.evaluate(signal)
        self.decisions.append(decision)
        self._emit(
            "observe_decision",
            {
                "kind": decision.kind.value,
                "reason_code": decision.reason_code,
                "signal_direction": decision.signal_direction.value,
                "evidence": decision.evidence,
            },
            causation_id=decision.causation_id,
            strategy_id=decision.strategy_id,
        )
        return decision

    def _on_reference_evaluate(self, event: ReferencePriceUpdated) -> None:
        # Advance fake clock to receive time so freshness is coherent in fixtures.
        if isinstance(self.clock, FakeClock):
            self.clock.set_utc(event.ts_received)
        market = self.registry.market
        if market is None:
            return
        yes = self.book_store.get(market.yes.instrument_id)
        no = self.book_store.get(market.no.instrument_id)
        if yes.initialized and not self._fact_seen("book_initialized_yes"):
            self._emit("book_state_initialized", {"side": "YES", "instrument_id": market.yes.instrument_id.value})
            self._mark("book_initialized_yes")
        if no.initialized and not self._fact_seen("book_initialized_no"):
            self._emit("book_state_initialized", {"side": "NO", "instrument_id": market.no.instrument_id.value})
            self._mark("book_initialized_no")
        ref = self.reference_store.get(self.config.binance_symbol)
        if ref.initialized and not self._fact_seen("reference_initialized"):
            self._emit(
                "reference_state_initialized",
                {"symbol": self.config.binance_symbol, "price": str(ref.snapshot.price) if ref.snapshot else None},
            )
            self._mark("reference_initialized")
        self.evaluate_once(causation_id=event.event_id)

    def _init_flags(self) -> None:
        self._flags: set[str] = set()

    def _fact_seen(self, key: str) -> bool:
        if not hasattr(self, "_flags"):
            self._init_flags()
        return key in self._flags

    def _mark(self, key: str) -> None:
        if not hasattr(self, "_flags"):
            self._init_flags()
        self._flags.add(key)

    def run_fixture(self) -> ObserveRunResult:
        if self.config.mode is not SourceMode.FIXTURE:
            raise ValueError("run_fixture requires fixture mode")
        assert self.config.fixture_path is not None
        self._attach()
        self._init_flags()
        self._emit(
            "runtime_start",
            {
                "mode": self.config.mode.value,
                "config_fingerprint": self.config.fingerprint(),
            },
        )
        try:
            market = load_market_from_fixture(self.config.fixture_path)
            self.registry.set_market(market)
            self._emit(
                "market_resolved",
                {
                    "market_id": market.market_id.value,
                    "condition_id": market.condition_id,
                    "yes_token": market.yes.token_id.value,
                    "no_token": market.no.token_id.value,
                    "question": market.question,
                    "event_slug": market.event_slug,
                },
            )
            pm = PolymarketFixtureSource(self.config.fixture_path)
            bn = BinanceFixtureSource(self.config.fixture_path)
            pm.publish_all(
                self.dispatcher,
                correlation_id=self.correlation_id,
                market_id=market.market_id,
            )
            bn.publish_all(
                self.dispatcher,
                correlation_id=self.correlation_id,
                symbol=self.config.binance_symbol,
            )
            self._emit("runtime_stop", {"decision_count": len(self.decisions)})
        except Exception as exc:
            self._emit("failure", {"error_type": type(exc).__name__, "message": str(exc)})
            self.sink.flush()
            raise
        finally:
            self.sink.flush()
        return ObserveRunResult(
            run_id=self.run_id,
            correlation_id=self.correlation_id,
            market=self.registry.require_market(),
            decisions=list(self.decisions),
            signals=list(self.signals),
            facts_path=self.sink.path,
            fact_count=self.sink.count,
        )

    def close(self) -> None:
        self.sink.close()


async def resolve_market_for_config(config: ObserveConfig) -> BinaryMarket:
    if config.mode is SourceMode.FIXTURE:
        assert config.fixture_path is not None
        return await FixtureMarketDiscovery().resolve_market(
            MarketRequest(fixture_path=str(config.fixture_path))
        )
    from tyrex_pm.adapters.polymarket.discovery import GammaMarketDiscovery

    return await GammaMarketDiscovery().resolve_market(
        MarketRequest(
            event_slug=config.event_slug,
            event_url=config.event_url,
            condition_id=config.condition_id,
        )
    )
