"""Pluggable OBSERVE strategy bindings — no isinstance(ZGapStrategy) in host.

Each binding owns signal/snapshot construction for its strategy kind and
returns a uniform evaluation result for the host loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.time_authority import ClockTimeAuthority, FakeTimeAuthority, TimeAuthority
from tyrex_pm.domain.polymarket.fees import FeeCurveParams, PROVISIONAL_SAMPLE_FEE
from tyrex_pm.domain.polymarket.ptb import PtbLockStore, PtbSnapshot
from tyrex_pm.indicators.ewma_volatility import EwmaVolatilityEstimator, SigmaConfig
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.signals.directional import DirectionalSignal, build_directional_signal
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyDecision
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ReferenceMomentumStrategy,
    TransitionResult,
)
from tyrex_pm.strategies.z_gap.assemble import assemble_zgap_decision_snapshot
from tyrex_pm.strategies.z_gap.calibration import build_calibration_row
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy
from tyrex_pm.strategies.z_gap.valuations import ZGapLeg, value_entry_leg


@dataclass
class StrategyEvalResult:
    decision: StrategyDecision
    intents: list[IntentLike]
    strategy_id: StrategyId
    signal_payload: dict[str, Any] = field(default_factory=dict)
    extra_facts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    # Momentum transition object when risk path needs apply_transition semantics
    momentum_transition: TransitionResult | None = None
    directional_signal: DirectionalSignal | None = None


class StrategyBinding(Protocol):
    strategy_id: StrategyId

    def on_start(self, context: StrategyContext) -> None: ...

    def on_stop(self, reason: str) -> None: ...

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        trigger: str,
        decision_context: DecisionContext | None,
        momentum_value: Decimal | None,
        momentum_ready: bool,
        momentum_reason: str,
        momentum_threshold: Decimal,
        max_book_spread: Decimal,
    ) -> StrategyEvalResult: ...

    @property
    def last_direction(self):  # optional, for suppress facts
        return None


@dataclass
class ReferenceMomentumBinding:
    """Existing momentum path behind the uniform binding surface."""

    strategy: ReferenceMomentumStrategy = field(default_factory=ReferenceMomentumStrategy)

    @property
    def strategy_id(self) -> StrategyId:
        return ReferenceMomentumStrategy.STRATEGY_ID

    @property
    def last_direction(self):
        return self.strategy.last_direction

    def on_start(self, context: StrategyContext) -> None:
        self.strategy.on_start(context)

    def on_stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        trigger: str,
        decision_context: DecisionContext | None,
        momentum_value: Decimal | None,
        momentum_ready: bool,
        momentum_reason: str,
        momentum_threshold: Decimal,
        max_book_spread: Decimal,
    ) -> StrategyEvalResult:
        signal = build_directional_signal(
            snapshot=market_snapshot,
            momentum=momentum_value,
            momentum_ready=momentum_ready,
            momentum_reason=momentum_reason,
            threshold=momentum_threshold,
            max_spread=max_book_spread,
        )
        signal_payload = {
            "direction": signal.direction.value,
            "reason_code": signal.reason_code,
            "momentum": None if signal.momentum is None else str(signal.momentum),
            "threshold": str(signal.threshold),
            "strength": None if signal.strength is None else str(signal.strength),
            "selected_outcome": None
            if signal.selected_outcome is None
            else signal.selected_outcome.value,
            "evidence": signal.evidence,
            "trigger": trigger,
        }
        if decision_context is None:
            decision = self.strategy.evaluate(signal)
            return StrategyEvalResult(
                decision=decision,
                intents=[],
                strategy_id=self.strategy_id,
                signal_payload=signal_payload,
                directional_signal=signal,
            )
        transition = self.strategy.apply_transition(signal, decision_context)
        return StrategyEvalResult(
            decision=transition.decision,
            intents=list(transition.intents),
            strategy_id=self.strategy_id,
            signal_payload=signal_payload,
            momentum_transition=transition,
            directional_signal=signal,
        )


@dataclass
class ZGapObserveBinding:
    """Z-Gap OBSERVE binding: EWMA + assemble + thin strategy."""

    strategy: ZGapStrategy
    ewma: EwmaVolatilityEstimator
    time_authority: TimeAuthority
    ptb_store: PtbLockStore
    fixture_ptb: PtbSnapshot | None
    fee_curve: FeeCurveParams
    fee_resolved: bool
    target_notional: Decimal
    window_id: str
    config: ZGapConfig
    _last_epoch_id: str | None = None

    @property
    def strategy_id(self) -> StrategyId:
        return ZGapStrategy.STRATEGY_ID

    @property
    def last_direction(self):
        return None

    def on_start(self, context: StrategyContext) -> None:
        self.strategy.on_start(context)
        if self.fixture_ptb is not None and self.fixture_ptb.k is not None:
            self.ptb_store.lock(self.fixture_ptb)

    def on_stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        trigger: str,
        decision_context: DecisionContext | None,
        momentum_value: Decimal | None,
        momentum_ready: bool,
        momentum_reason: str,
        momentum_threshold: Decimal,
        max_book_spread: Decimal,
    ) -> StrategyEvalResult:
        # Update EWMA from reference (host-owned estimator; strategy does not)
        if market_snapshot.reference is not None:
            self.ewma.update(
                market_snapshot.reference.price,
                market_snapshot.reference.ts_event,
            )
        vol = self.ewma.snapshot()
        time_view = self.time_authority.view()

        ptb = None
        if self.fixture_ptb is not None:
            ptb = self.ptb_store.observe(self.fixture_ptb)

        decision_input = assemble_zgap_decision_snapshot(
            market_snapshot=market_snapshot,
            vol=vol,
            ptb=ptb,
            time_view=time_view,
            config=self.config,
            fee_curve=self.fee_curve,
            fee_resolved=self.fee_resolved,
            target_notional=self.target_notional,
            trigger=trigger if trigger in {"feed", "timer"} else "feed",
            correlation_id=correlation_id,
            causation_id=causation_id,
            window_id=self.window_id,
            settlement_ref=(
                None
                if market_snapshot.reference is None
                else market_snapshot.reference.price
            ),
            settlement_ref_fresh=market_snapshot.reference_freshness.is_fresh,
        )

        # Same-epoch guard (duplicate trigger)
        if self._last_epoch_id == decision_input.epoch.epoch_id:
            # Should be unique per assemble; still protect lineage
            pass
        self._last_epoch_id = decision_input.epoch.epoch_id

        if decision_context is None:
            raise ValueError("Z-Gap OBSERVE binding requires DecisionContext from host")
        decision, intents = self.strategy.on_decision(decision_input, decision_context)

        up_val = value_entry_leg(
            model=decision_input.model,
            leg=ZGapLeg.UP,
            book=decision_input.up_book,
            config=self.config,
            fee_curve=self.fee_curve,
        )
        down_val = value_entry_leg(
            model=decision_input.model,
            leg=ZGapLeg.DOWN,
            book=decision_input.down_book,
            config=self.config,
            fee_curve=self.fee_curve,
        )
        calib = build_calibration_row(
            decision_input=decision_input,
            decision=decision,
            up_val=up_val,
            down_val=down_val,
            actionable=bool(intents),
        )

        extra_facts = [
            (
                "zgap_model_snapshot",
                {
                    "epoch_id": decision_input.epoch.epoch_id,
                    "window_id": decision_input.window_id,
                    "ready": decision_input.model.ready,
                    "z": decision_input.model.z,
                    "p_up": decision_input.model.p_up,
                    "p_down": decision_input.model.p_down,
                    "sigma": decision_input.model.sigma,
                    "tau_s": decision_input.model.tau_s,
                    "K": None if decision_input.model.K is None else str(decision_input.model.K),
                    "S": None if decision_input.model.S is None else str(decision_input.model.S),
                    "basis_bps": None
                    if decision_input.model.basis_bps is None
                    else str(decision_input.model.basis_bps),
                    "reject_reasons": list(decision_input.model.reject_reasons),
                    "trigger": decision_input.trigger,
                    "ptb_quality": None
                    if decision_input.ptb is None
                    else decision_input.ptb.quality.value,
                    "time_ready": decision_input.time.ready,
                    "time_uncertainty_ms": decision_input.time.uncertainty_ms,
                    "economics_label": "estimated",
                },
            ),
            (
                "zgap_entry_valuation",
                {
                    "epoch_id": decision_input.epoch.epoch_id,
                    "up": {
                        "e_settlement": None
                        if up_val.e_settlement is None
                        else str(up_val.e_settlement),
                        "e_repricing": None
                        if up_val.e_repricing is None
                        else str(up_val.e_repricing),
                        "ask": None
                        if up_val.executable_ask is None
                        else str(up_val.executable_ask),
                        "ready": up_val.ready,
                    },
                    "down": {
                        "e_settlement": None
                        if down_val.e_settlement is None
                        else str(down_val.e_settlement),
                        "e_repricing": None
                        if down_val.e_repricing is None
                        else str(down_val.e_repricing),
                        "ask": None
                        if down_val.executable_ask is None
                        else str(down_val.executable_ask),
                        "ready": down_val.ready,
                    },
                    "valuation_label": "counterfactual",
                    "economics_label": "estimated",
                },
            ),
            ("zgap_calibration_row", calib),
        ]

        signal_payload = {
            "signal_type": "z_gap_decision",
            "epoch_id": decision_input.epoch.epoch_id,
            "window_id": decision_input.window_id,
            "trigger": decision_input.trigger,
            "model_ready": decision_input.model.ready,
            "z": decision_input.model.z,
            "p_up": decision_input.model.p_up,
            "p_down": decision_input.model.p_down,
        }

        return StrategyEvalResult(
            decision=decision,
            intents=list(intents),
            strategy_id=self.strategy_id,
            signal_payload=signal_payload,
            extra_facts=extra_facts,
        )


def zgap_config_from_runtime(zg) -> ZGapConfig:
    """Map ObserveConfig.z_gap runtime knobs → pure ZGapConfig."""
    from tyrex_pm.strategies.z_gap.config import (
        ZGapEntryConfig,
        ZGapFrictionConfig,
        ZGapPtbTimeQualityConfig,
        ZGapRealizationConfig,
        ZGapVolatilityConfig,
    )

    return ZGapConfig(
        volatility=ZGapVolatilityConfig(
            half_life_s=zg.half_life_s,
            min_samples_s=zg.min_samples_s,
            sample_interval_s=zg.sample_interval_s,
            tau_floor_s=zg.tau_floor_s,
        ),
        entry=ZGapEntryConfig(
            theta_take=zg.theta_take,
            z_min=zg.z_min,
            z_max=zg.z_max,
            tau_min_s=zg.tau_min_s,
            tau_max_s=zg.tau_max_s,
            expected_slippage_buy=zg.expected_slippage_buy,
            exit_friction_reserve=Decimal("0"),
            reject_both_legs_edge=zg.reject_both_legs_edge,
        ),
        realization=ZGapRealizationConfig(
            expected_slippage_sell=zg.expected_slippage_sell,
            slippage_included_in_executable_bid=True,
        ),
        ptb_time_quality=ZGapPtbTimeQualityConfig(basis_max_bps=zg.basis_max_bps),
        friction=ZGapFrictionConfig(
            fee_curve=FeeCurveParams(fee_rate=zg.fee_rate, exponent=zg.fee_exponent)
        ),
    )


def build_strategy_binding(
    *,
    strategy_kind: str,
    zgap_runtime=None,
    zgap_config: ZGapConfig | None = None,
    fixture_ptb: PtbSnapshot | None = None,
    fee_curve: FeeCurveParams | None = None,
    fee_resolved: bool = True,
    target_notional: Decimal = Decimal("5"),
    window_id: str = "default",
    time_authority: TimeAuthority | None = None,
    clock=None,
) -> StrategyBinding:
    kind = strategy_kind.strip().lower().replace("-", "_")
    if kind in {"reference_momentum", "momentum"}:
        return ReferenceMomentumBinding()
    if kind in {"z_gap", "zgap"}:
        if zgap_config is None and zgap_runtime is not None:
            zgap_config = zgap_config_from_runtime(zgap_runtime)
            target_notional = zgap_runtime.target_notional
            window_id = zgap_runtime.window_id
            fee_curve = FeeCurveParams(
                fee_rate=zgap_runtime.fee_rate, exponent=zgap_runtime.fee_exponent
            )
        cfg = zgap_config or ZGapConfig()
        ewma = EwmaVolatilityEstimator(
            SigmaConfig(
                half_life_s=cfg.volatility.half_life_s,
                min_samples_s=cfg.volatility.min_samples_s,
                jump_threshold_sigma=cfg.volatility.jump_threshold_sigma,
                sample_interval_s=cfg.volatility.sample_interval_s,
                tau_floor_s=cfg.volatility.tau_floor_s,
            )
        )
        auth: TimeAuthority
        if time_authority is not None:
            auth = time_authority
        elif clock is not None:
            from tyrex_pm.core.clock import FakeClock

            if isinstance(clock, FakeClock):
                auth = FakeTimeAuthority(clock=clock)
            else:
                auth = ClockTimeAuthority(clock=clock)
        else:
            raise ValueError("z_gap binding requires time_authority or clock")
        return ZGapObserveBinding(
            strategy=ZGapStrategy(config=cfg),
            ewma=ewma,
            time_authority=auth,
            ptb_store=PtbLockStore(),
            fixture_ptb=fixture_ptb,
            fee_curve=fee_curve or PROVISIONAL_SAMPLE_FEE,
            fee_resolved=fee_resolved,
            target_notional=target_notional,
            window_id=window_id,
            config=cfg,
        )
    raise ValueError(f"unsupported strategy_kind: {strategy_kind!r}")
