"""Thin Z-Gap strategy coordinator (F3/F4).

Orchestrates F2 valuation/policy only. No formulas, adapters, OMS, or stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from tyrex_pm.core.ids import StrategyId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, new_intent_id
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState
from tyrex_pm.strategies.context import DecisionContext, StrategyContext
from tyrex_pm.strategies.decisions import IntentLike, StrategyAction, StrategyDecision
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.decision_input import ZGapDecisionSnapshot
from tyrex_pm.strategies.z_gap.policies import (
    NormalizedRiskFlags,
    combine_precedence,
    evaluate_readiness,
    evaluate_realization,
    evaluate_thesis,
    evaluate_time_resolution,
    select_leg,
)
from tyrex_pm.strategies.z_gap.reasons import ZGapReason
from tyrex_pm.strategies.z_gap.state import ThesisConfirmPhase, ThesisConfirmState
from tyrex_pm.strategies.z_gap.valuations import (
    ZGapLeg,
    value_entry_leg,
    value_position,
)

_EXIT_BUSY = frozenset(
    {
        LifecycleState.EXIT_REQUESTED,
        LifecycleState.EXIT_PENDING,
        LifecycleState.EXIT_RETRY_WAIT,
        LifecycleState.MANUAL_INTERVENTION,
    }
)


@dataclass
class ZGapStrategy:
    """Thin bridge: immutable decision input → F1 StrategyDecision + intents."""

    STRATEGY_ID = StrategyId("z_gap")

    config: ZGapConfig = field(default_factory=ZGapConfig)
    _started: bool = False
    _window_id: str | None = None
    _entry_lineage_consumed: bool = False
    _window_closed_to_reentry: bool = False
    _thesis_state: ThesisConfirmState = field(default_factory=ThesisConfirmState)
    _last_epoch_id: str | None = None
    _decision_epoch_counter: int = 0

    def on_start(self, context: StrategyContext) -> None:
        self._started = True
        self._window_id = None
        self._entry_lineage_consumed = False
        self._window_closed_to_reentry = False
        self._thesis_state = ThesisConfirmState()
        self._last_epoch_id = None
        self._decision_epoch_counter = 0

    def on_stop(self, reason: str) -> None:
        self._started = False

    def bind_window(self, window_id: str) -> None:
        if self._window_id != window_id:
            self._window_id = window_id
            self._entry_lineage_consumed = False
            self._window_closed_to_reentry = False
            self._thesis_state = ThesisConfirmState()
            self._decision_epoch_counter = 0

    def bump_decision_epoch(self) -> None:
        """Host lifecycle hook after return to FLAT — does not reopen entry lineage."""
        self._decision_epoch_counter += 1

    @property
    def decision_epoch(self) -> int:
        return self._decision_epoch_counter

    @property
    def last_direction(self):
        return None

    def restore_state(
        self,
        *,
        decision_epoch: int = 0,
        last_direction=None,
        entry_lineage_consumed: bool = False,
        window_closed_to_reentry: bool = False,
        window_id: str | None = None,
        thesis_state: dict[str, Any] | None = None,
    ) -> None:
        """Framework-owned recovery of the minimal strategy-private slice."""
        del last_direction  # unused; Z-Gap has no directional last state
        self._decision_epoch_counter = int(decision_epoch)
        self._entry_lineage_consumed = bool(entry_lineage_consumed)
        self._window_closed_to_reentry = bool(window_closed_to_reentry)
        if window_id is not None:
            self._window_id = window_id
        if thesis_state is not None:
            phase_raw = thesis_state.get("phase", ThesisConfirmPhase.IDLE.value)
            try:
                phase = ThesisConfirmPhase(str(phase_raw))
            except ValueError:
                phase = ThesisConfirmPhase.IDLE
            self._thesis_state = ThesisConfirmState(
                phase=phase,
                adverse_since_mono_ns=thesis_state.get("adverse_since_mono_ns"),
                last_p_held=thesis_state.get("last_p_held"),
            )

    def persistence_slice(self) -> dict[str, Any]:
        return {
            "strategy_epoch": self._decision_epoch_counter,
            "entry_lineage_consumed": self._entry_lineage_consumed,
            "window_closed_to_reentry": self._window_closed_to_reentry,
            "window_id": self._window_id,
            "thesis_state": {
                "phase": self._thesis_state.phase.value,
                "adverse_since_mono_ns": self._thesis_state.adverse_since_mono_ns,
                "last_p_held": self._thesis_state.last_p_held,
            },
        }

    def on_decision(
        self,
        decision_input: ZGapDecisionSnapshot,
        context: DecisionContext,
    ) -> tuple[StrategyDecision, list[IntentLike]]:
        """Primary decision entrypoint — consumes one atomic snapshot."""
        self.bind_window(decision_input.window_id)
        self._decision_epoch_counter += 1
        self._last_epoch_id = decision_input.epoch.epoch_id

        flags = NormalizedRiskFlags(
            unknown_inventory=bool(
                decision_input.capabilities.get("unknown_inventory", False)
            )
            or bool(context.unknown_inventory),
            emergency=bool(decision_input.capabilities.get("emergency", False)),
            kill_switch=context.kill_switch_active,
        )

        basis = None
        from tyrex_pm.indicators.reference_basis import BasisResult, BasisValidity

        if decision_input.model.basis_bps is not None:
            basis = BasisResult(
                basis_bps=decision_input.model.basis_bps,
                validity=BasisValidity.VALID,
                ready=True,
                reason_code=None,
                trading_ref=decision_input.model.S,
                settlement_ref=decision_input.model.S,
                trading_ref_fresh=True,
                settlement_ref_fresh=True,
            )
        elif decision_input.evidence.get("basis_validity") not in (None, "VALID"):
            basis = BasisResult(
                basis_bps=None,
                validity=BasisValidity.STALE,
                ready=False,
                reason_code="basis_not_valid",
                trading_ref=decision_input.model.S,
                settlement_ref=None,
                trading_ref_fresh=False,
                settlement_ref_fresh=False,
            )

        ready = evaluate_readiness(
            model=decision_input.model,
            ptb=decision_input.ptb,
            basis=basis,
            time_ready=decision_input.time.ready,
            config=self.config,
        )

        if not decision_input.fee_resolved:
            decision = self._decision(
                action=StrategyAction.WAIT,
                reason=ZGapReason.FEE_NOT_READY,
                decision_input=decision_input,
                evidence={"fee_resolved": False},
            )
            return decision, []

        if decision_input.position is not None and decision_input.position.confirmed_quantity > 0:
            return self._evaluate_active(decision_input, context, flags)

        if not ready.ready:
            decision = self._decision(
                action=ready.action,
                reason=ready.reason_code,
                decision_input=decision_input,
                evidence=dict(ready.evidence),
            )
            return decision, []

        # Lifecycle may already own an entry attempt — do not emit a new lineage.
        if context.lifecycle is not None and context.lifecycle.state is LifecycleState.ENTRY_PENDING:
            decision = self._decision(
                action=StrategyAction.HOLD,
                reason=ZGapReason.SKIP,
                decision_input=decision_input,
                evidence={"lifecycle": "ENTRY_PENDING"},
            )
            return decision, []

        if self._entry_lineage_consumed or self._window_closed_to_reentry:
            decision = self._decision(
                action=StrategyAction.SKIP,
                reason=ZGapReason.SKIP,
                decision_input=decision_input,
                evidence={
                    "entry_lineage_consumed": self._entry_lineage_consumed,
                    "window_closed": self._window_closed_to_reentry,
                    "note": (
                        "strategy entry lineage consumed; lifecycle may retry "
                        "the same attempt, but no new semantic entry"
                    ),
                },
            )
            return decision, []

        if not context.entry_allowed:
            decision = self._decision(
                action=StrategyAction.SKIP,
                reason=ZGapReason.SKIP,
                decision_input=decision_input,
                evidence={
                    "entry_block_reason": context.entry_block_reason,
                },
            )
            return decision, []

        up_val = value_entry_leg(
            model=decision_input.model,
            leg=ZGapLeg.UP,
            book=decision_input.up_book,
            config=self.config,
            fee_curve=decision_input.fee_curve,
        )
        down_val = value_entry_leg(
            model=decision_input.model,
            leg=ZGapLeg.DOWN,
            book=decision_input.down_book,
            config=self.config,
            fee_curve=decision_input.fee_curve,
        )
        selection = select_leg(up_val, down_val, config=self.config)
        policy = combine_precedence(
            flags=flags,
            model_valid=decision_input.model.ready,
            thesis=None,
            realization=None,
            time_res=None,
            entry_selection=selection,
            flat=True,
        )

        evidence: dict[str, Any] = {
            **dict(decision_input.evidence),
            "trigger": decision_input.trigger,
            "epoch_id": decision_input.epoch.epoch_id,
            "up_valuation": _valuation_evidence(up_val),
            "down_valuation": _valuation_evidence(down_val),
            "selection": dict(selection.evidence),
            "selected_leg": None if selection.selected is None else selection.selected.value,
            "economics_label": "estimated",
            "valuation_label": "counterfactual",
        }

        intents: list[IntentLike] = []
        if policy.action is StrategyAction.ENTER and selection.selected is not None:
            intent = self._make_enter_intent(
                decision_input=decision_input,
                context=context,
                leg=selection.selected,
                up_val=up_val,
                down_val=down_val,
            )
            intents.append(intent)
            # Strategy entry lineage consumed at emit — not on fill.
            # Lifecycle-owned retries of the same attempt are framework-owned.
            self._entry_lineage_consumed = True
            self._window_closed_to_reentry = True
            evidence["intent_id"] = intent.intent_id.value
            if context.mode is RuntimeMode.OBSERVE:
                evidence["counterfactual_note"] = (
                    "OBSERVE records would-enter intent only; no fill/portfolio"
                )

        decision = self._decision(
            action=policy.action,
            reason=policy.reason_code,
            decision_input=decision_input,
            evidence=evidence,
        )
        return decision, intents

    def _evaluate_active(
        self,
        decision_input: ZGapDecisionSnapshot,
        context: DecisionContext,
        flags: NormalizedRiskFlags,
    ) -> tuple[StrategyDecision, list[IntentLike]]:
        assert decision_input.position is not None
        held = decision_input.position.held_leg
        book = (
            decision_input.up_book if held is ZGapLeg.UP else decision_input.down_book
        )
        pos_val = value_position(
            model=decision_input.model,
            position=decision_input.position,
            book=book,
            config=self.config,
            fee_curve=decision_input.fee_curve,
        )
        p_held = None if pos_val.p_held is None else float(pos_val.p_held)
        thesis = evaluate_thesis(
            p_held=p_held,
            model_valid=decision_input.model.ready,
            model_fresh=bool(decision_input.model.freshness.get("reference", True)),
            now_mono_ns=decision_input.time.monotonic_ns,
            prior=self._thesis_state,
            config=self.config,
        )
        self._thesis_state = thesis.state
        realization = evaluate_realization(pos_val, config=self.config)
        time_res = evaluate_time_resolution(
            tau_s=decision_input.model.tau_s,
            resolution_capability=bool(
                decision_input.capabilities.get("resolution_capability", False)
            ),
            v_sell=pos_val.v_sell,
            v_resolve_adj=pos_val.v_resolve_adj,
            config=self.config,
        )
        policy = combine_precedence(
            flags=flags,
            model_valid=decision_input.model.ready,
            thesis=thesis,
            realization=realization,
            time_res=time_res,
            entry_selection=None,
            flat=False,
        )
        evidence: dict[str, Any] = {
            "trigger": decision_input.trigger,
            "epoch_id": decision_input.epoch.epoch_id,
            "position_valuation": {
                "held_leg": held.value,
                "confirmed_quantity": str(decision_input.position.confirmed_quantity),
                "entry_cost_total": str(decision_input.position.entry_cost_total),
                "market_richness": None
                if pos_val.market_richness is None
                else str(pos_val.market_richness),
                "v_sell": None if pos_val.v_sell is None else str(pos_val.v_sell),
                "p_held": None if pos_val.p_held is None else str(pos_val.p_held),
                "remaining_hold_edge": None
                if pos_val.remaining_hold_edge is None
                else str(pos_val.remaining_hold_edge),
                "pnl_liquidation_estimated": None
                if pos_val.pnl_liquidation is None
                else str(pos_val.pnl_liquidation),
                "label": "shadow_position" if context.mode is RuntimeMode.SHADOW else "hypothetical",
                "economics_label": "estimated",
                "pnl_label": "estimated_shadow_pnl",
            },
            "thesis": thesis.reason_code.value,
            "thesis_confirming": thesis.confirming,
            "realization": realization.reason_code.value,
            "time_resolution": time_res.reason_code.value,
            "exit_outstanding": bool(
                context.lifecycle is not None and context.lifecycle.state in _EXIT_BUSY
            ),
        }

        intents: list[IntentLike] = []
        emit_exits = self._may_emit_exit_intents(decision_input, context)
        if emit_exits and policy.action in {StrategyAction.EXIT, StrategyAction.FLATTEN}:
            escalate = (
                policy.action is StrategyAction.FLATTEN
                or context.exit_escalate
                or context.kill_switch_active
            )
            life = context.lifecycle
            exit_busy = life is not None and life.state in _EXIT_BUSY
            if exit_busy and not escalate and not context.exit_escalate:
                evidence["exit_suppressed"] = "EXIT_ALREADY_OUTSTANDING"
            elif not context.exit_allowed and not escalate:
                evidence["exit_suppressed"] = context.exit_block_reason or "EXIT_BLOCKED"
            else:
                intent = self._make_exit_intent(
                    decision_input=decision_input,
                    context=context,
                    held=held,
                    action=policy.action,
                    reason=policy.reason_code,
                )
                intents.append(intent)
                evidence["intent_id"] = intent.intent_id.value

        decision = self._decision(
            action=policy.action,
            reason=policy.reason_code,
            decision_input=decision_input,
            evidence=evidence,
        )
        return decision, intents

    def _may_emit_exit_intents(
        self, decision_input: ZGapDecisionSnapshot, context: DecisionContext
    ) -> bool:
        if bool(decision_input.capabilities.get("emit_exit_intents", False)):
            return True
        return context.mode is RuntimeMode.SHADOW

    def _make_exit_intent(
        self,
        *,
        decision_input: ZGapDecisionSnapshot,
        context: DecisionContext,
        held: ZGapLeg,
        action: StrategyAction,
        reason: ZGapReason,
    ) -> ExitIntent | FlattenIntent:
        market = context.snapshot.market
        instrument = market.yes if held is ZGapLeg.UP else market.no
        common = dict(
            intent_id=new_intent_id(),
            strategy_id=self.STRATEGY_ID,
            instrument_id=instrument.instrument_id,
            market_id=market.market_id,
            created_at=decision_input.observed_at,
            correlation_id=decision_input.correlation_id,
            causation_id=decision_input.causation_id,
            reason_code=reason.value,
            evidence={
                "held_leg": held.value,
                "epoch_id": decision_input.epoch.epoch_id,
                "target_flat": True,
                "economics_label": "estimated",
                "quantity_source": "confirmed_internal_portfolio",
            },
        )
        if action is StrategyAction.FLATTEN:
            return FlattenIntent(**common, urgency="URGENT")
        return ExitIntent(**common, target_flat=True)

    def _make_enter_intent(
        self,
        *,
        decision_input: ZGapDecisionSnapshot,
        context: DecisionContext,
        leg: ZGapLeg,
        up_val,
        down_val,
    ) -> EnterIntent:
        market = context.snapshot.market
        if leg is ZGapLeg.UP:
            instrument = market.yes
            outcome = OutcomeSide.YES
            val = up_val
        else:
            instrument = market.no
            outcome = OutcomeSide.NO
            val = down_val
        now = decision_input.observed_at
        max_price = val.max_economic_price
        return EnterIntent(
            intent_id=new_intent_id(),
            strategy_id=self.STRATEGY_ID,
            instrument_id=instrument.instrument_id,
            market_id=market.market_id,
            created_at=now,
            correlation_id=decision_input.correlation_id,
            causation_id=decision_input.causation_id,
            reason_code=ZGapReason.ENTRY_CANDIDATE.value,
            evidence={
                "selected_leg": leg.value,
                "epoch_id": decision_input.epoch.epoch_id,
                "e_settlement": None
                if val.e_settlement is None
                else str(val.e_settlement),
                "e_repricing": None if val.e_repricing is None else str(val.e_repricing),
                "executable_ask": None
                if val.executable_ask is None
                else str(val.executable_ask),
                "c_entry_unit": None if val.c_entry_unit is None else str(val.c_entry_unit),
                "economics_label": "estimated",
                "valuation_label": "counterfactual"
                if context.mode is RuntimeMode.OBSERVE
                else "planned",
                "observe_semantics": "would_enter_not_filled"
                if context.mode is RuntimeMode.OBSERVE
                else "shadow_entry_request",
            },
            target_notional=decision_input.target_notional,
            outcome=outcome,
            decision_epoch=self._decision_epoch_counter,
            max_price=max_price,
        )

    def _decision(
        self,
        *,
        action: StrategyAction,
        reason: ZGapReason,
        decision_input: ZGapDecisionSnapshot,
        evidence: dict[str, Any],
    ) -> StrategyDecision:
        return StrategyDecision(
            action=action,
            reason_code=reason.value,
            decided_at=decision_input.observed_at,
            correlation_id=decision_input.correlation_id,
            causation_id=decision_input.causation_id,
            strategy_id=self.STRATEGY_ID,
            evidence={
                **evidence,
                "window_id": decision_input.window_id,
                "market_id": decision_input.market_id.value,
                "trigger": decision_input.trigger,
                "z": decision_input.model.z,
                "p_up": decision_input.model.p_up,
                "p_down": decision_input.model.p_down,
                "tau_s": decision_input.model.tau_s,
                "sigma": decision_input.model.sigma,
            },
        )


def _valuation_evidence(val) -> dict[str, Any]:
    return {
        "ready": val.ready,
        "leg": val.leg.value,
        "p": None if val.fair_probability is None else str(val.fair_probability),
        "ask": None if val.executable_ask is None else str(val.executable_ask),
        "c_entry_unit": None if val.c_entry_unit is None else str(val.c_entry_unit),
        "e_settlement": None if val.e_settlement is None else str(val.e_settlement),
        "e_repricing": None if val.e_repricing is None else str(val.e_repricing),
        "reason_codes": list(val.reason_codes),
        "economics_label": "estimated",
        "valuation_label": "counterfactual",
    }
