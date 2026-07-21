"""Pluggable strategy bindings — uniform host-facing evaluation interface.

Host
→ binding.evaluate(trigger, immutable framework context)
→ StrategyDecision + list[IntentLike]

The composition layer knows which assembler/strategy to invoke. Generic hosts
must not branch on strategy_kind or isinstance(strategy, …) inside evaluation,
intent dispatch, risk, planning, or lifecycle logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.time_authority import ClockTimeAuthority, FakeTimeAuthority, TimeAuthority
from tyrex_pm.domain.polymarket.fees import FeeCurveParams, PROVISIONAL_SAMPLE_FEE
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.ptb import PtbLockStore, PtbSnapshot, make_fixture_ptb
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
from tyrex_pm.strategies.z_gap.valuations import PositionView, ZGapLeg, value_entry_leg


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

    def on_market_bound(self, market: BinaryMarket, *, clock_now) -> None:
        """Optional market/window binding (PTB fixture, etc.). Default no-op."""
        ...

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

    def bump_decision_epoch(self) -> None: ...

    def persistence_slice(self) -> dict[str, Any]: ...

    def restore_persistence_slice(self, data: dict[str, Any]) -> None: ...

    @property
    def last_direction(self):  # optional, for suppress facts
        return None

    @property
    def timer_eval_count(self) -> int:
        return 0

    @property
    def evaluate_interval_s(self) -> float:
        return 1.0


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

    @property
    def timer_eval_count(self) -> int:
        return 0

    @property
    def evaluate_interval_s(self) -> float:
        return 1.0

    def on_start(self, context: StrategyContext) -> None:
        self.strategy.on_start(context)

    def on_stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)

    def on_market_bound(self, market: BinaryMarket, *, clock_now) -> None:
        return None

    def bump_decision_epoch(self) -> None:
        self.strategy.bump_decision_epoch()

    def persistence_slice(self) -> dict[str, Any]:
        return {
            "strategy_epoch": self.strategy.decision_epoch,
            "strategy_last_direction": None
            if self.strategy.last_direction is None
            else self.strategy.last_direction.value,
        }

    def restore_persistence_slice(self, data: dict[str, Any]) -> None:
        from tyrex_pm.signals.directional import Direction

        last_dir = data.get("strategy_last_direction")
        self.strategy.restore_state(
            decision_epoch=int(data.get("strategy_epoch") or 0),
            last_direction=None if last_dir is None else Direction(last_dir),
        )

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
        del correlation_id  # carried on market_snapshot / signal
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
class ZGapBinding:
    """Z-Gap binding: EWMA + assemble + thin strategy (OBSERVE and SHADOW)."""

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
    _timer_eval_count: int = 2
    _evaluate_interval_s: float = 1.0
    _last_epoch_id: str | None = None

    @property
    def strategy_id(self) -> StrategyId:
        return ZGapStrategy.STRATEGY_ID

    @property
    def last_direction(self):
        return None

    @property
    def timer_eval_count(self) -> int:
        return self._timer_eval_count

    @property
    def evaluate_interval_s(self) -> float:
        return self._evaluate_interval_s

    def on_start(self, context: StrategyContext) -> None:
        self.strategy.on_start(context)
        if self.fixture_ptb is not None and self.fixture_ptb.k is not None:
            self.ptb_store.lock(self.fixture_ptb)

    def on_stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)

    def on_market_bound(self, market: BinaryMarket, *, clock_now) -> None:
        if self.fixture_ptb is not None:
            return
        if market.event_start is None or market.event_end is None:
            return
        # Host may have already set fixture_ptb via configure_fixture_ptb.

    def configure_fixture_ptb(self, ptb: PtbSnapshot) -> None:
        self.fixture_ptb = ptb
        self.window_id = ptb.window_id

    def bump_decision_epoch(self) -> None:
        self.strategy.bump_decision_epoch()

    def persistence_slice(self) -> dict[str, Any]:
        return self.strategy.persistence_slice()

    def restore_persistence_slice(self, data: dict[str, Any]) -> None:
        self.strategy.restore_state(
            decision_epoch=int(data.get("strategy_epoch") or 0),
            entry_lineage_consumed=bool(data.get("entry_lineage_consumed", False)),
            window_closed_to_reentry=bool(data.get("window_closed_to_reentry", False)),
            window_id=data.get("window_id"),
            thesis_state=data.get("thesis_state"),
        )

    def _position_from_context(
        self, context: DecisionContext, *, epoch_placeholder_needed: bool
    ) -> PositionView | None:
        del epoch_placeholder_needed
        qty = context.position_quantity
        if qty is None or qty <= 0:
            return None
        life = context.lifecycle
        if life is None or life.instrument_id is None:
            return None
        market = context.snapshot.market
        if life.instrument_id == market.yes.instrument_id:
            held = ZGapLeg.UP
        elif life.instrument_id == market.no.instrument_id:
            held = ZGapLeg.DOWN
        else:
            return None
        # Epoch is rebound inside assemble to the sealed decision epoch.
        from tyrex_pm.strategies.z_gap.snapshots import DecisionEpoch

        placeholder = DecisionEpoch.new(
            market_id=market.market_id,
            window_id=self.window_id,
            evaluated_at=context.snapshot.observed_at,
            correlation_id=context.snapshot.correlation_id,
            causation_id=context.snapshot.causation_id,
        )
        return PositionView(
            epoch=placeholder,
            held_leg=held,
            confirmed_quantity=qty,
            entry_cost_total=context.position_cost_total,
        )

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
        settlement_ref: Decimal | None = None,
        settlement_ref_fresh: bool | None = None,
        volatility_price: Decimal | None = None,
        volatility_ts=None,
    ) -> StrategyEvalResult:
        del momentum_value, momentum_ready, momentum_reason, momentum_threshold, max_book_spread
        # High-frequency returns for sigma may use a different series than model S
        # (N4: Binance raw for EWMA; C_hat on market_snapshot.reference for fair value).
        if volatility_price is not None and volatility_ts is not None:
            self.ewma.update(volatility_price, volatility_ts)
        elif market_snapshot.reference is not None:
            self.ewma.update(
                market_snapshot.reference.price,
                market_snapshot.reference.ts_event,
            )
        vol = self.ewma.snapshot()
        time_view = self.time_authority.view()

        ptb = None
        if self.fixture_ptb is not None:
            ptb = self.ptb_store.observe(self.fixture_ptb)

        if decision_context is None:
            raise ValueError("Z-Gap binding requires DecisionContext from host")

        position = self._position_from_context(
            decision_context, epoch_placeholder_needed=True
        )
        capabilities = {
            "resolution_capability": bool(
                decision_context.resolution_capability.available
            ),
            "unknown_inventory": bool(decision_context.unknown_inventory),
        }

        if settlement_ref is None:
            settlement_ref = (
                None
                if market_snapshot.reference is None
                else market_snapshot.reference.price
            )
            settlement_ref_fresh = market_snapshot.reference_freshness.is_fresh
        elif settlement_ref_fresh is None:
            settlement_ref_fresh = market_snapshot.reference_freshness.is_fresh

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
            settlement_ref=settlement_ref,
            settlement_ref_fresh=bool(settlement_ref_fresh),
            position=position,
            capabilities=capabilities,
        )

        self._last_epoch_id = decision_input.epoch.epoch_id
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

        pos_payload = None
        if decision_input.position is not None:
            pos_payload = {
                "held_leg": decision_input.position.held_leg.value,
                "confirmed_quantity": str(decision_input.position.confirmed_quantity),
                "entry_cost_total": str(decision_input.position.entry_cost_total),
                "truth_source": "portfolio_lifecycle",
            }

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
                    "position": pos_payload,
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
        if pos_payload is not None:
            extra_facts.append(
                (
                    "zgap_active_position_context",
                    {
                        "epoch_id": decision_input.epoch.epoch_id,
                        **pos_payload,
                        "action": decision.action.value,
                        "reason_code": decision.reason_code,
                    },
                )
            )

        signal_payload = {
            "signal_type": "z_gap_decision",
            "epoch_id": decision_input.epoch.epoch_id,
            "window_id": decision_input.window_id,
            "trigger": decision_input.trigger,
            "model_ready": decision_input.model.ready,
            "z": decision_input.model.z,
            "p_up": decision_input.model.p_up,
            "p_down": decision_input.model.p_down,
            "has_position": pos_payload is not None,
        }

        return StrategyEvalResult(
            decision=decision,
            intents=list(intents),
            strategy_id=self.strategy_id,
            signal_payload=signal_payload,
            extra_facts=extra_facts,
        )


# Backward-compatible alias
ZGapObserveBinding = ZGapBinding


def zgap_config_from_runtime(zg) -> ZGapConfig:
    """Map ObserveConfig.z_gap runtime knobs → pure ZGapConfig."""
    from tyrex_pm.strategies.z_gap.config import (
        ZGapEntryConfig,
        ZGapFrictionConfig,
        ZGapPtbTimeQualityConfig,
        ZGapRealizationConfig,
        ZGapThesisConfig,
        ZGapTimeResolutionConfig,
        ZGapVolatilityConfig,
    )

    return ZGapConfig(
        volatility=ZGapVolatilityConfig(
            half_life_s=zg.half_life_s,
            min_samples_s=zg.min_samples_s,
            sample_interval_s=zg.sample_interval_s,
            tau_floor_s=zg.tau_floor_s,
            jump_threshold_sigma=float(getattr(zg, "jump_threshold_sigma", 4.0)),
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
            theta_rich=getattr(zg, "theta_rich", Decimal("0.02")),
            expected_slippage_sell=zg.expected_slippage_sell,
            slippage_included_in_executable_bid=True,
        ),
        thesis=ZGapThesisConfig(
            p_stop=getattr(zg, "p_stop", Decimal("0.4013")),
            stop_confirm_s=float(getattr(zg, "stop_confirm_s", 1.0)),
        ),
        time_resolution=ZGapTimeResolutionConfig(
            flatten_before_event_end_s=float(
                getattr(zg, "flatten_before_event_end_s", 20.0)
            ),
            resolution_capability_default=False,
            ponr_before_event_end_s=float(
                getattr(zg, "ponr_before_event_end_s", 5.0)
            ),
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
        timer_count = 2
        interval_s = 1.0
        if zgap_config is None and zgap_runtime is not None:
            zgap_config = zgap_config_from_runtime(zgap_runtime)
            target_notional = zgap_runtime.target_notional
            window_id = zgap_runtime.window_id
            fee_curve = FeeCurveParams(
                fee_rate=zgap_runtime.fee_rate, exponent=zgap_runtime.fee_exponent
            )
            timer_count = zgap_runtime.timer_eval_count
            interval_s = zgap_runtime.evaluate_interval_s
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
        return ZGapBinding(
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
            _timer_eval_count=timer_count,
            _evaluate_interval_s=interval_s,
        )
    raise ValueError(f"unsupported strategy_kind: {strategy_kind!r}")


def bind_fixture_ptb_for_market(
    binding: StrategyBinding,
    *,
    market: BinaryMarket,
    window_id: str,
    k: Decimal,
    receive_ts,
) -> None:
    """Composition helper: attach fixture PTB without host strategy-type branching."""
    configure = getattr(binding, "configure_fixture_ptb", None)
    if configure is None:
        return
    if market.event_start is None or market.event_end is None:
        return
    ptb = make_fixture_ptb(
        market_id=market.market_id,
        window_id=window_id,
        event_start=market.event_start,
        event_end=market.event_end,
        k=k,
        receive_ts=receive_ts,
        provenance_ref="fixture_f4",
    )
    configure(ptb)
