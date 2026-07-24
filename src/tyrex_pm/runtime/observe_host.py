"""Composition root for observe + R4 intent/risk/plan path."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
from tyrex_pm.core.events import EventSource, ReferencePriceUpdated, TimerElapsed
from tyrex_pm.core.ids import (
    CorrelationId,
    RunId,
    new_correlation_id,
    new_event_id,
    new_run_id,
)
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.domain.polymarket.market import BinaryMarket, MarketRequest
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.indicators.momentum import ShortHorizonMomentum
from tyrex_pm.market_data.book_store import MarketStateStore
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import book_quote
from tyrex_pm.market_data.freshness import assess_freshness
from tyrex_pm.market_data.reference_store import ReferenceDataStore
from tyrex_pm.market_data.registry import InstrumentRegistry
from tyrex_pm.planning.plan import PlanningResult, PlanStatus
from tyrex_pm.planning.planner import ExecutionPlanner
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.jsonl import JsonlFactSink
from tyrex_pm.risk.context import BookReadiness, RiskConfigView, RiskContext
from tyrex_pm.risk.decision import RiskDecision
from tyrex_pm.risk.dedup import IntentDedupRegistry
from tyrex_pm.risk.engine import RiskEngine
from tyrex_pm.risk.reasons import RiskReason
from tyrex_pm.runtime.config import ObserveConfig, SourceMode
from tyrex_pm.runtime.strategy_binding import (
    StrategyBinding,
    StrategyEvalResult,
    bind_fixture_ptb_for_market,
    build_strategy_binding,
)
from tyrex_pm.signals.directional import DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyDecision


@dataclass
class ObserveRunResult:
    run_id: RunId
    correlation_id: CorrelationId
    market: BinaryMarket
    decisions: list[StrategyDecision] = field(default_factory=list)
    signals: list[DirectionalSignal] = field(default_factory=list)
    intents: list[IntentLike] = field(default_factory=list)
    risk_decisions: list[RiskDecision] = field(default_factory=list)
    plans: list[PlanningResult] = field(default_factory=list)
    facts_path: Path | None = None
    fact_count: int = 0


class ObserveHost:
    """Wires clock, dispatcher, stores, indicators, strategy, risk, planner, reporting."""

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
        self.binding: StrategyBinding = self._build_binding()
        # Compatibility alias for tests/shadow that still touch host.strategy
        self.strategy = getattr(self.binding, "strategy", self.binding)
        self.risk_engine = RiskEngine()
        self.planner = ExecutionPlanner()
        from datetime import timedelta as _td

        self.dedup = IntentDedupRegistry(
            lifetime=(
                config.risk.duplicate_lifetime
                if config.risk is not None
                else _td(hours=1)
            )
        )
        self.sink = JsonlFactSink(config.output_path)
        self.decisions: list[StrategyDecision] = []
        self.signals: list[DirectionalSignal] = []
        self.intents: list[IntentLike] = []
        self.risk_decisions: list[RiskDecision] = []
        self.plans: list[PlanningResult] = []
        self._attached = False
        self._kill_switch = bool(config.risk.kill_switch_active) if config.risk else False
        self._timer_fire_count = 0
        self._last_eval_trigger = "feed"

    def _build_binding(self) -> StrategyBinding:
        # Composition-time registry only — evaluation path never branches on kind.
        target = (
            self.config.risk.target_notional
            if self.config.risk is not None
            else (
                self.config.z_gap.target_notional
                if self.config.z_gap is not None
                else Decimal("5")
            )
        )
        return build_strategy_binding(
            strategy_kind=self.config.strategy_kind,
            zgap_runtime=self.config.z_gap,
            zgap_config=self.config.zgap_pure,
            fixture_ptb=None,
            clock=self.clock,
            target_notional=target,
            window_id=(
                self.config.z_gap.window_id if self.config.z_gap is not None else "default"
            ),
        )

    def set_kill_switch(self, active: bool) -> None:
        prev = self._kill_switch
        self._kill_switch = active
        if prev != active:
            self._emit(
                "kill_switch_state_change",
                {"active": active, "previous": prev},
            )

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

    def _runtime_mode(self) -> RuntimeMode:
        if self.config.risk is None:
            return RuntimeMode.OBSERVE
        return self.config.risk.runtime_mode

    def _build_risk_context(self, snapshot: DecisionSnapshot) -> RiskContext:
        market = snapshot.market
        yes_state = self.book_store.get(market.yes.instrument_id)
        no_state = self.book_store.get(market.no.instrument_id)
        risk_cfg = self.config.risk
        assert risk_cfg is not None
        return RiskContext(
            mode=risk_cfg.runtime_mode,
            now=self.clock.now_utc(),
            market=market,
            snapshot=snapshot,
            yes_quote=snapshot.yes_quote,
            no_quote=snapshot.no_quote,
            yes_book=BookReadiness(
                initialized=yes_state.initialized,
                recovery_required=yes_state.recovery_required,
                tick_size=yes_state.tick_size or market.tick_size,
            ),
            no_book=BookReadiness(
                initialized=no_state.initialized,
                recovery_required=no_state.recovery_required,
                tick_size=no_state.tick_size or market.tick_size,
            ),
            risk_config=RiskConfigView(
                max_notional=risk_cfg.max_notional,
                min_price=risk_cfg.min_price,
                max_price=risk_cfg.max_price,
                max_spread=risk_cfg.max_spread,
                min_liquidity_notional=risk_cfg.min_liquidity_notional,
                no_entry_before_close=risk_cfg.no_entry_before_close,
                kill_switch_active=self._kill_switch,
                config_fingerprint=risk_cfg.fingerprint(),
            ),
            dedup=self.dedup,
            exposure_available=False,
        )

    def _handle_transition(
        self,
        signal: DirectionalSignal,
        snapshot: DecisionSnapshot,
        transition,
    ) -> None:
        if transition.suppressed and not transition.intents:
            if transition.suppress_reason in {
                "REPEATED_DIRECTION",
                "REVERSAL_NO_PORTFOLIO",
                "EXIT_DEFERRED_TO_R5",
            }:
                self._emit(
                    "intent_suppressed",
                    {
                        "reason": transition.suppress_reason,
                        "signal_direction": signal.direction.value,
                        "last_direction": None
                        if getattr(self.binding, "last_direction", None) is None
                        else self.binding.last_direction.value,
                    },
                    causation_id=signal.causation_id,
                    strategy_id=self.binding.strategy_id,
                )
            return

        for intent in transition.intents:
            self.intents.append(intent)
            self._emit(
                "intent_created",
                {
                    "intent_id": intent.intent_id.value,
                    "kind": intent.kind.value,
                    "instrument_id": intent.instrument_id.value,
                    "market_id": intent.market_id.value,
                    "target_notional": str(intent.target_notional),
                    "outcome": intent.outcome.value,
                    "semantic_key": intent.semantic_key(),
                    "reason_code": intent.reason_code,
                },
                causation_id=intent.causation_id,
                strategy_id=intent.strategy_id,
            )
            risk_ctx = self._build_risk_context(snapshot)
            decision = self.risk_engine.evaluate(intent, risk_ctx)
            self.risk_decisions.append(decision)
            self._emit(
                "risk_decision",
                {
                    "decision_id": decision.decision_id.value,
                    "intent_id": decision.intent_id.value,
                    "approved": decision.approved,
                    "mode": decision.mode.value,
                    "reason_codes": [r.value for r in decision.reason_codes],
                    "policy_results": [
                        {
                            "policy_id": p.policy_id,
                            "approved": p.approved,
                            "reason_code": p.reason_code.value,
                            "evidence": dict(p.evidence),
                        }
                        for p in decision.policy_results
                    ],
                    "config_fingerprint": decision.config_fingerprint,
                },
                causation_id=decision.causation_id,
            )
            if decision.approved:
                self._emit(
                    "risk_approved",
                    {"decision_id": decision.decision_id.value, "intent_id": intent.intent_id.value},
                    causation_id=decision.causation_id,
                )
                book = (
                    snapshot.yes_book
                    if intent.instrument_id == snapshot.market.yes.instrument_id
                    else snapshot.no_book
                )
                plan_result = self.planner.plan(
                    intent,
                    risk=decision,
                    market=snapshot.market,
                    book=book,
                    now=self.clock.now_utc(),
                    causation_id=intent.causation_id,
                )
                self.plans.append(plan_result)
                if plan_result.status is PlanStatus.PLANNED and plan_result.plan is not None:
                    p = plan_result.plan
                    self._emit(
                        "execution_plan_created",
                        {
                            "plan_id": p.plan_id.value,
                            "intent_id": p.intent_id.value,
                            "quantity": str(p.quantity),
                            "limit_price": str(p.limit_price),
                            "expected_notional": str(p.expected_notional),
                            "status": plan_result.status.value,
                        },
                        causation_id=p.causation_id,
                    )
                else:
                    self.dedup.forget(intent.semantic_key())
                    self._emit(
                        "planning_failed",
                        {
                            "intent_id": intent.intent_id.value,
                            "status": plan_result.status.value,
                            "fail_reason": None
                            if plan_result.fail_reason is None
                            else plan_result.fail_reason.value,
                            "evidence": dict(plan_result.evidence),
                        },
                        causation_id=intent.causation_id,
                    )
            else:
                self._emit(
                    "risk_denied",
                    {
                        "decision_id": decision.decision_id.value,
                        "intent_id": intent.intent_id.value,
                        "reason_codes": [r.value for r in decision.reason_codes],
                    },
                    causation_id=decision.causation_id,
                )
                if RiskReason.LIVE_NOT_SUPPORTED in decision.reason_codes:
                    self._emit(
                        "unsupported_live_mode",
                        {"mode": decision.mode.value},
                        causation_id=decision.causation_id,
                    )
                if RiskReason.DUPLICATE_INTENT in decision.reason_codes:
                    self._emit(
                        "duplicate_intent",
                        {
                            "intent_id": intent.intent_id.value,
                            "semantic_key": intent.semantic_key(),
                            "evidence": dict(
                                next(
                                    (
                                        p.evidence
                                        for p in decision.policy_results
                                        if p.reason_code is RiskReason.DUPLICATE_INTENT
                                    ),
                                    {},
                                )
                            ),
                        },
                        causation_id=decision.causation_id,
                    )

    def evaluate_once(
        self, *, causation_id=None, trigger: str | None = None
    ) -> StrategyDecision:
        """Uniform path: snapshot → binding.evaluate → intent dispatch hooks."""
        eval_trigger = trigger or self._last_eval_trigger or "feed"
        self._last_eval_trigger = eval_trigger
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
                "trigger": eval_trigger,
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

        context = self._build_decision_context(snapshot)
        result = self.binding.evaluate(
            market_snapshot=snapshot,
            causation_id=causation_id,
            correlation_id=self.correlation_id,
            trigger=eval_trigger,
            decision_context=context,
            momentum_value=momentum_value,
            momentum_ready=momentum_ready,
            momentum_reason=momentum_reason,
            momentum_threshold=self.config.momentum_threshold,
            max_book_spread=self.config.max_book_spread,
        )
        if result.directional_signal is not None:
            self.signals.append(result.directional_signal)
        self._emit(
            "signal",
            result.signal_payload,
            causation_id=causation_id,
            strategy_id=result.strategy_id,
        )
        for fact_type, payload in result.extra_facts:
            self._emit(
                fact_type,
                payload,
                causation_id=causation_id,
                strategy_id=result.strategy_id,
            )
        self.decisions.append(result.decision)
        self._emit_observe_decision(result.decision)
        self._dispatch_eval_result(result, snapshot)
        return result.decision

    def _compose_resolution_capability(self):
        from tyrex_pm.domain.polymarket.resolution_capability import (
            DISABLED_RESOLUTION,
            ResolutionCapability,
            ResolutionCapabilityStatus,
        )

        zg = self.config.z_gap
        if zg is None or not bool(getattr(zg, "resolution_capability", False)):
            return DISABLED_RESOLUTION
        return ResolutionCapability(
            status=ResolutionCapabilityStatus.AVAILABLE,
            ponr_before_event_end_s=float(
                getattr(zg, "ponr_before_event_end_s", 5.0)
            ),
        )

    def _ponr_reached(self, snapshot: DecisionSnapshot) -> bool:
        cap = self._compose_resolution_capability()
        if not cap.available:
            return False
        now = self.clock.now_utc()
        tau = (snapshot.market.event_end - now).total_seconds()
        return tau <= cap.ponr_before_event_end_s

    def _build_decision_context(
        self, snapshot: DecisionSnapshot, signal: DirectionalSignal | None = None
    ) -> DecisionContext | None:
        """Immutable decision context hook.

        Returns ``None`` when risk is not configured and the binding tolerates
        evaluate-only mode (momentum). Z-Gap bindings require a context; when
        risk is absent we still supply a minimal OBSERVE context.
        """
        del signal  # legacy signature compatibility; direction comes from binding
        target = (
            self.config.risk.target_notional
            if self.config.risk is not None
            else (
                self.config.z_gap.target_notional
                if self.config.z_gap is not None
                else Decimal("5")
            )
        )
        cap = self._compose_resolution_capability()
        ponr = self._ponr_reached(snapshot)
        if self.config.risk is None:
            # Momentum evaluate-only: None → binding uses strategy.evaluate
            # Z-Gap always needs a context — supply OBSERVE shell.
            if getattr(self.binding, "timer_eval_count", 0) > 0 or self.config.z_gap is not None:
                return DecisionContext(
                    run_id=self.run_id,
                    mode=RuntimeMode.OBSERVE,
                    snapshot=snapshot,
                    target_notional=target,
                    max_price=None,
                    kill_switch_active=self._kill_switch,
                    now=self.clock.now_utc(),
                    resolution_capability=cap,
                    ponr_reached=ponr,
                )
            return None
        return DecisionContext(
            run_id=self.run_id,
            mode=self.config.risk.runtime_mode,
            snapshot=snapshot,
            target_notional=target,
            max_price=self.config.risk.max_price,
            kill_switch_active=self._kill_switch,
            now=self.clock.now_utc(),
            resolution_capability=cap,
            ponr_reached=ponr,
        )

    def _dispatch_eval_result(
        self, result: StrategyEvalResult, snapshot: DecisionSnapshot
    ) -> None:
        """Mode-agnostic post-eval dispatch (dry / observe-record / OMS overrides)."""
        if result.momentum_transition is not None and result.directional_signal is not None:
            self._process_transition(
                result.directional_signal, snapshot, result.momentum_transition
            )
            return
        if result.intents:
            self._process_intents(result.intents, snapshot)

    def _process_intents(
        self, intents: list[IntentLike], snapshot: DecisionSnapshot
    ) -> None:
        """OBSERVE default: record intents only (no OMS). ShadowHost overrides."""
        del snapshot
        self._record_observe_intents(intents)

    def _record_observe_intents(self, intents: list[IntentLike]) -> None:
        """Record economic intents as facts only — no OMS, fills, or portfolio."""
        for intent in intents:
            self.intents.append(intent)
            payload = {
                "intent_id": intent.intent_id.value,
                "kind": intent.kind.value,
                "instrument_id": intent.instrument_id.value,
                "market_id": intent.market_id.value,
                "semantic_key": intent.semantic_key(),
                "reason_code": intent.reason_code,
                "observe_only": True,
                "oms_submit": False,
                "economics_label": "estimated",
                "valuation_label": "counterfactual",
            }
            if hasattr(intent, "target_notional"):
                payload["target_notional"] = str(intent.target_notional)
            if hasattr(intent, "outcome"):
                payload["outcome"] = intent.outcome.value
            if hasattr(intent, "window_id"):
                payload["window_id"] = intent.window_id
            self._emit(
                "intent_created",
                payload,
                causation_id=intent.causation_id,
                strategy_id=intent.strategy_id,
            )
            self._emit(
                "intent_observe_no_oms",
                {
                    "intent_id": intent.intent_id.value,
                    "note": "OBSERVE records would-intent only; no order/fill/portfolio",
                },
                causation_id=intent.causation_id,
                strategy_id=intent.strategy_id,
            )

    def _process_transition(
        self,
        signal: DirectionalSignal,
        snapshot: DecisionSnapshot,
        transition,
    ) -> None:
        """Dry (R4) intent/risk/plan processing hook; overridden for shadow OMS."""
        self._handle_transition(signal, snapshot, transition)

    def _emit_observe_decision(self, decision: StrategyDecision) -> None:
        # Emit both neutral action and validation_kind (when present) for continuity.
        self._emit(
            "observe_decision",
            {
                "action": decision.action.value,
                "kind": decision.evidence.get("validation_kind", decision.action.value),
                "reason_code": decision.reason_code,
                "signal_direction": decision.evidence.get("signal_direction"),
                "decision_id": decision.decision_id,
                "evidence": dict(decision.evidence),
            },
            causation_id=decision.causation_id,
            strategy_id=decision.strategy_id,
        )

    def _on_reference_evaluate(self, event: ReferencePriceUpdated) -> None:
        if isinstance(self.clock, FakeClock):
            self.clock.set_utc(event.ts_received)
        market = self.registry.market
        if market is None:
            return
        yes = self.book_store.get(market.yes.instrument_id)
        no = self.book_store.get(market.no.instrument_id)
        if yes.initialized and not self._fact_seen("book_initialized_yes"):
            self._emit(
                "book_state_initialized",
                {"side": "YES", "instrument_id": market.yes.instrument_id.value},
            )
            self._mark("book_initialized_yes")
        if no.initialized and not self._fact_seen("book_initialized_no"):
            self._emit(
                "book_state_initialized",
                {"side": "NO", "instrument_id": market.no.instrument_id.value},
            )
            self._mark("book_initialized_no")
        ref = self.reference_store.get(self.config.binance_symbol)
        if ref.initialized and not self._fact_seen("reference_initialized"):
            self._emit(
                "reference_state_initialized",
                {
                    "symbol": self.config.binance_symbol,
                    "price": str(ref.snapshot.price) if ref.snapshot else None,
                },
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

    def _start_strategy(self, market: BinaryMarket) -> None:
        mode = self._runtime_mode()
        if self.config.z_gap is not None:
            bind_fixture_ptb_for_market(
                self.binding,
                market=market,
                window_id=self.config.z_gap.window_id,
                k=self.config.z_gap.ptb_k,
                receive_ts=self.clock.now_utc(),
            )
        self.binding.on_market_bound(market, clock_now=self.clock.now_utc())
        self.binding.on_start(
            StrategyContext(
                run_id=self.run_id,
                strategy_id=self.binding.strategy_id,
                mode=mode,
                market=market,
            )
        )
        self.dedup.reset_market()

    def _run_timer_evaluations(self) -> None:
        """Host-owned deterministic timer → same evaluate path as feed triggers."""
        count = int(getattr(self.binding, "timer_eval_count", 0) or 0)
        interval_s = float(getattr(self.binding, "evaluate_interval_s", 1.0) or 1.0)
        if count <= 0:
            return
        if not isinstance(self.clock, FakeClock):
            return
        from datetime import timedelta as _td

        for _ in range(count):
            # Advance wall and monotonic together so thesis confirmation can complete.
            self.clock.advance(
                wall=_td(seconds=interval_s),
                mono_ns=int(interval_s * 1_000_000_000),
            )
            self._timer_fire_count += 1
            timer_event = TimerElapsed(
                event_id=new_event_id(),
                ts_event=self.clock.now_utc(),
                ts_received=self.clock.now_utc(),
                source=EventSource.TIMER,
                correlation_id=self.correlation_id,
                timer_name="strategy_eval",
                fire_count=self._timer_fire_count,
            )
            self._emit(
                "timer_elapsed",
                {
                    "timer_name": timer_event.timer_name,
                    "fire_count": timer_event.fire_count,
                },
                causation_id=timer_event.event_id,
                strategy_id=self.binding.strategy_id,
            )
            self.evaluate_once(
                causation_id=timer_event.event_id, trigger="timer"
            )

    def _publish_fixture_timeline(self, market: BinaryMarket) -> None:
        """Publish fixture market + reference events interleaved by ts_received."""
        assert self.config.fixture_path is not None
        from tyrex_pm.adapters.binance.fixture_source import _parse_ts as _bn_ts
        from tyrex_pm.adapters.binance.normalize import normalize_trade_message
        from tyrex_pm.adapters.polymarket.fixture_source import _parse_ts as _pm_ts
        from tyrex_pm.adapters.polymarket.normalize import normalize_market_ws_message

        pm = PolymarketFixtureSource(self.config.fixture_path)
        bn = BinanceFixtureSource(self.config.fixture_path)
        timeline: list[tuple[datetime, str, dict]] = []
        for item in pm._payload.get("polymarket_events", []):
            timeline.append((_pm_ts(item.get("ts_received") or item.get("timestamp")), "pm", item))
        for item in bn._payload.get("binance_events", []):
            timeline.append(
                (
                    _bn_ts(item.get("ts_received") or item.get("T") or item.get("timestamp")),
                    "bn",
                    item,
                )
            )
        timeline.sort(key=lambda row: (row[0], 0 if row[1] == "pm" else 1))

        last_wall: datetime | None = None
        for ts_received, kind, item in timeline:
            if isinstance(self.clock, FakeClock):
                if last_wall is not None and ts_received > last_wall:
                    delta = ts_received - last_wall
                    self.clock.advance(
                        wall=delta,
                        mono_ns=int(delta.total_seconds() * 1_000_000_000),
                    )
                else:
                    self.clock.set_utc(ts_received)
                last_wall = ts_received
            body = item.get("payload") or item
            if kind == "pm":
                event = normalize_market_ws_message(
                    body,
                    ts_received=ts_received,
                    correlation_id=self.correlation_id,
                    market_id=market.market_id,
                )
                if event is not None:
                    self.dispatcher.publish(event)
            else:
                event = normalize_trade_message(
                    body,
                    ts_received=ts_received,
                    correlation_id=self.correlation_id,
                    symbol=self.config.binance_symbol,
                )
                if event is not None:
                    self.dispatcher.publish(event)

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
                "runtime_mode": self._runtime_mode().value,
                "config_fingerprint": self.config.fingerprint(),
                "strategy_kind": self.config.strategy_kind,
            },
        )
        try:
            market = load_market_from_fixture(self.config.fixture_path)
            self.registry.set_market(market)
            self._start_strategy(market)
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
            self._publish_fixture_timeline(market)
            self._run_timer_evaluations()
            self.binding.on_stop("NORMAL")
            self._emit(
                "runtime_stop",
                {
                    "decision_count": len(self.decisions),
                    "intent_count": len(self.intents),
                    "risk_count": len(self.risk_decisions),
                    "plan_count": len(self.plans),
                    "strategy_kind": self.config.strategy_kind,
                },
            )
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
            intents=list(self.intents),
            risk_decisions=list(self.risk_decisions),
            plans=list(self.plans),
            facts_path=self.sink.path,
            fact_count=self.sink.count,
        )

    def close(self) -> None:
        self.sink.close()


# R5.1 host unification: ObserveHost is the single orchestration path
# (snapshot -> freshness -> indicator -> signal -> strategy). Mode (dry vs
# shadow OMS) only changes what ``_build_decision_context``/``_process_transition``
# do; ``ShadowHost`` overrides those hooks rather than duplicating the pipeline.
TradingHost = ObserveHost


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
