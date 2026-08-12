"""Z-Gap data preparation and strategy evaluation adapter."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId, StrategyId
from tyrex_pm.core.time_authority import ClockTimeAuthority, FakeTimeAuthority, TimeAuthority
from tyrex_pm.domain.polymarket.fees import PROVISIONAL_SAMPLE_FEE, FeeCurveParams
from tyrex_pm.domain.polymarket.ptb import PtbLockStore, PtbSnapshot
from tyrex_pm.facts.contract import EligibilityFacts
from tyrex_pm.indicators.ewma_volatility import EwmaVolatilityEstimator, SigmaConfig
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.evaluation import StrategyEvaluation
from tyrex_pm.strategies.z_gap.assemble import assemble_zgap_decision_snapshot
from tyrex_pm.strategies.z_gap.calibration import build_calibration_row
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.snapshots import DecisionEpoch
from tyrex_pm.strategies.z_gap.strategy import ZGapStrategy
from tyrex_pm.strategies.z_gap.valuations import PositionView, ZGapLeg, value_entry_leg

__all__ = ["StrategyEvaluation", "ZGapDriver", "create_z_gap_driver"]


@dataclass
class ZGapDriver:
    strategy: ZGapStrategy
    volatility: EwmaVolatilityEstimator
    time_authority: TimeAuthority
    ptb_store: PtbLockStore
    fee_curve: FeeCurveParams
    target_notional: Decimal
    window_id: str
    config: ZGapConfig
    ptb: PtbSnapshot | None = None

    @property
    def strategy_id(self) -> StrategyId:
        return ZGapStrategy.STRATEGY_ID

    def on_start(self, context: StrategyContext) -> None:
        self.strategy.on_start(context)

    def on_stop(self, reason: str) -> None:
        self.strategy.on_stop(reason)

    def configure_ptb(self, ptb: PtbSnapshot) -> None:
        self.ptb = ptb
        self.window_id = ptb.window_id
        self.ptb_store.lock(ptb)

    def ingest_volatility(self, price: Decimal | str | int, timestamp):  # noqa: ANN001
        return self.volatility.update(price, timestamp)

    def seed_volatility(self, observations, *, now_ts=None):  # noqa: ANN001
        return self.volatility.seed_observations(observations, now_ts=now_ts)

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

    def _position(
        self, snapshot: DecisionSnapshot, context: DecisionContext
    ) -> PositionView | None:
        quantity = context.position_quantity
        lifecycle = context.lifecycle
        if quantity <= 0 or lifecycle is None or lifecycle.instrument_id is None:
            return None
        if lifecycle.instrument_id == snapshot.market.yes.instrument_id:
            held = ZGapLeg.UP
        elif lifecycle.instrument_id == snapshot.market.no.instrument_id:
            held = ZGapLeg.DOWN
        else:
            return None
        epoch = DecisionEpoch.new(
            market_id=snapshot.market.market_id,
            window_id=self.window_id,
            evaluated_at=snapshot.observed_at,
            correlation_id=snapshot.correlation_id,
            causation_id=snapshot.causation_id,
        )
        return PositionView(
            epoch=epoch,
            held_leg=held,
            confirmed_quantity=quantity,
            entry_cost_total=context.position_cost_total,
        )

    def evaluate(
        self,
        *,
        market_snapshot: DecisionSnapshot,
        causation_id: EventId | None,
        correlation_id: CorrelationId,
        context: DecisionContext,
        trigger: str,
        settlement_reference: Decimal | None,
        settlement_reference_fresh: bool,
    ) -> StrategyEvaluation:
        vol = self.volatility.snapshot()
        time_view = self.time_authority.view()
        ptb = None if self.ptb is None else self.ptb_store.observe(self.ptb)
        decision_input = assemble_zgap_decision_snapshot(
            market_snapshot=market_snapshot,
            vol=vol,
            ptb=ptb,
            time_view=time_view,
            config=self.config,
            fee_curve=self.fee_curve,
            fee_resolved=True,
            target_notional=self.target_notional,
            trigger=trigger,
            correlation_id=correlation_id,
            causation_id=causation_id,
            window_id=self.window_id,
            settlement_ref=settlement_reference,
            settlement_ref_fresh=settlement_reference_fresh,
            position=self._position(market_snapshot, context),
            capabilities={
                "resolution_capability": context.resolution_capability.available,
                "unknown_inventory": context.unknown_inventory,
                "emit_exit_intents": True,
            },
        )
        decision, intents = self.strategy.on_decision(decision_input, context)
        up = value_entry_leg(
            model=decision_input.model,
            leg=ZGapLeg.UP,
            book=decision_input.up_book,
            config=self.config,
            fee_curve=self.fee_curve,
        )
        down = value_entry_leg(
            model=decision_input.model,
            leg=ZGapLeg.DOWN,
            book=decision_input.down_book,
            config=self.config,
            fee_curve=self.fee_curve,
        )
        decision_evidence = dict(decision.evidence)
        raw_blockers = decision_evidence.get("blockers", ())
        if not isinstance(raw_blockers, (list, tuple)):
            raw_blockers = ()
        blockers = tuple(str(value) for value in raw_blockers)
        model_ready = bool(decision_input.model.ready)
        eligible = bool(
            decision_evidence.get(
                "strategy_inputs_eligible",
                model_ready and not blockers,
            )
        )
        return StrategyEvaluation(
            decision=decision,
            intents=tuple(intents),
            strategy_id=self.strategy_id,
            eligibility=EligibilityFacts(
                strategy_inputs_eligible=eligible,
                model_ready=model_ready,
                blockers=blockers,
            ),
            reporting_context={
                "decision_input": decision_input,
                "calibration": build_calibration_row(
                    decision_input=decision_input,
                    decision=decision,
                    up_val=up,
                    down_val=down,
                    actionable=bool(intents),
                ),
            },
        )


def create_z_gap_driver(
    *,
    config: ZGapConfig,
    time_authority: TimeAuthority | None = None,
    clock=None,
    target_notional: Decimal = Decimal("5"),
    window_id: str = "unbound",
    fee_curve: FeeCurveParams | None = None,
) -> ZGapDriver:
    if time_authority is None:
        if clock is None:
            raise ValueError("Z-Gap driver requires a time authority or clock")
        from tyrex_pm.core.clock import FakeClock

        time_authority = (
            FakeTimeAuthority(clock=clock)
            if isinstance(clock, FakeClock)
            else ClockTimeAuthority(clock=clock)
        )
    return ZGapDriver(
        strategy=ZGapStrategy(config=config),
        volatility=EwmaVolatilityEstimator(
            SigmaConfig(
                half_life_s=config.volatility.half_life_s,
                min_samples_s=config.volatility.min_samples_s,
                jump_threshold_sigma=config.volatility.jump_threshold_sigma,
                sample_interval_s=config.volatility.sample_interval_s,
                tau_floor_s=config.volatility.tau_floor_s,
                sigma_floor=config.volatility.sigma_floor,
            )
        ),
        time_authority=time_authority,
        ptb_store=PtbLockStore(),
        fee_curve=fee_curve or PROVISIONAL_SAMPLE_FEE,
        target_notional=target_notional,
        window_id=window_id,
        config=config,
    )
