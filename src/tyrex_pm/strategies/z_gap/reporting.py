"""Z-Gap strategy diagnostics builder — emit already-calculated values only."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.reporting.contracts import StrategyDiagnosticsBlob
from tyrex_pm.strategies.decisions import StrategyDecision
from tyrex_pm.strategies.z_gap.calibration import build_calibration_row
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.decision_input import ZGapDecisionSnapshot
from tyrex_pm.strategies.z_gap.policies import LegSelectionResult, ReadinessResult
from tyrex_pm.strategies.z_gap.reasons import ZGapReason
from tyrex_pm.strategies.z_gap.valuations import EntryLegValuation

DIAGNOSTICS_NAMESPACE = "z_gap"
DIAGNOSTICS_SCHEMA_VERSION = "1.0"
STRATEGY_VERSION = "1.0.0"

# Gates in evaluation order (later ones are not_evaluated once an earlier gate fails).
_READINESS_GATE_ORDER = (
    "TIME_READY",
    "PTB_USABLE",
    "PTB_LAG",
    "JUMP_GUARD",
    "MODEL_READY",
    "BASIS",
    "TAU_IN_BAND",
    "ABS_Z_BLOCK",
    "Z_IN_BAND",
)
_SELECTION_GATE_ORDER = (
    "BOOK_FEE_READY",
    "EDGE_VS_THETA",
)


class ZGapDiagnosticsContract:
    """Composition-time registration object for RunReporter."""

    namespace = DIAGNOSTICS_NAMESPACE
    schema_version = DIAGNOSTICS_SCHEMA_VERSION
    strategy_id = "z_gap"
    strategy_version = STRATEGY_VERSION

    def __init__(self, config: ZGapConfig | None = None) -> None:
        self._config = config

    def build_diagnostics(self, context: Mapping[str, Any]) -> StrategyDiagnosticsBlob:
        return build_zgap_diagnostics_blob(
            decision_input=context["decision_input"],
            decision=context["decision"],
            up_val=context.get("up_val"),
            down_val=context.get("down_val"),
            readiness=context.get("readiness"),
            selection=context.get("selection"),
            config=context.get("config") or self._config,
            actionable=bool(context.get("actionable")),
        )

    def build_gates(self, context: Mapping[str, Any]) -> list[dict[str, Any]]:
        return build_zgap_gates(
            decision_input=context["decision_input"],
            decision=context["decision"],
            readiness=context.get("readiness"),
            selection=context.get("selection"),
            up_val=context.get("up_val"),
            down_val=context.get("down_val"),
            config=context.get("config") or self._config,
        )

    def closest_candidate_fields(
        self, context: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        return build_closest_candidate_fields(
            decision_input=context["decision_input"],
            decision=context["decision"],
            up_val=context.get("up_val"),
            down_val=context.get("down_val"),
            selection=context.get("selection"),
            readiness=context.get("readiness"),
            config=context.get("config") or self._config,
        )


def build_zgap_diagnostics_blob(
    *,
    decision_input: ZGapDecisionSnapshot,
    decision: StrategyDecision,
    up_val: EntryLegValuation | None = None,
    down_val: EntryLegValuation | None = None,
    readiness: ReadinessResult | None = None,
    selection: LegSelectionResult | None = None,
    config: ZGapConfig | None = None,
    actionable: bool = False,
) -> StrategyDiagnosticsBlob:
    calib = build_calibration_row(
        decision_input=decision_input,
        decision=decision,
        up_val=up_val,
        down_val=down_val,
        actionable=actionable,
    )
    model = decision_input.model
    values: dict[str, Any] = {
        **calib,
        "strategy_version": STRATEGY_VERSION,
        "readiness_reason": None if readiness is None else readiness.reason_code.value,
        "selection_reason": None if selection is None else selection.reason_code.value,
        "jump_guard_tripped": model.jump_guard_tripped,
        "model_ready": model.ready,
        "model_reject_reasons": list(model.reject_reasons),
        "ptb_quality": None
        if decision_input.ptb is None
        else decision_input.ptb.quality.value,
        "ptb_usable": None
        if decision_input.ptb is None
        else bool(decision_input.ptb.usable_for_entry),
        "target_notional": str(decision_input.target_notional),
        "source_timestamps": {
            k: v.isoformat() for k, v in dict(model.source_timestamps).items()
        },
        "freshness": dict(model.freshness),
    }
    if config is not None:
        values["theta_take"] = str(config.entry.theta_take)
        values["theta_fill_floor"] = str(config.entry.theta_fill_floor)
        values["z_min"] = str(config.entry.z_min)
        values["z_max"] = str(config.entry.z_max)
        values["tau_min_s"] = config.entry.tau_min_s
        values["tau_max_s"] = config.entry.tau_max_s
        values["require_repricing_edge"] = config.entry.require_repricing_edge
        values["slippage_included_in_executable_bid"] = (
            config.realization.slippage_included_in_executable_bid
        )
    return StrategyDiagnosticsBlob(
        namespace=DIAGNOSTICS_NAMESPACE,
        schema_version=DIAGNOSTICS_SCHEMA_VERSION,
        values=values,
    )


def build_zgap_gates(
    *,
    decision_input: ZGapDecisionSnapshot,
    decision: StrategyDecision,
    readiness: ReadinessResult | None,
    selection: LegSelectionResult | None,
    up_val: EntryLegValuation | None,
    down_val: EntryLegValuation | None,
    config: ZGapConfig | None,
) -> list[dict[str, Any]]:
    """Record gate outcomes already implied by readiness/selection — no recompute of z/σ."""
    model = decision_input.model
    gates: list[dict[str, Any]] = []
    failed_reason = None if readiness is None else readiness.reason_code
    if failed_reason is None:
        try:
            failed_reason = ZGapReason(decision.reason_code)
        except ValueError:
            failed_reason = None
    # Selection was reached if reason is a selection/entry family code.
    selection_reasons = {
        ZGapReason.NO_ECONOMIC_EDGE,
        ZGapReason.BELOW_THRESHOLD,
        ZGapReason.TIE_AMBIGUOUS,
        ZGapReason.BOTH_LEGS_EDGE,
        ZGapReason.ENTRY_CANDIDATE,
        ZGapReason.BOOK_NOT_READY,
        ZGapReason.FEE_NOT_READY,
        ZGapReason.LEG_NOT_READY,
    }
    reached_selection = (readiness is not None and readiness.ready) or (
        failed_reason in selection_reasons
        or decision.action.value in {"ENTER", "HOLD", "EXIT", "FLATTEN"}
    )
    if readiness is not None and not readiness.ready:
        reached_selection = False

    def add(
        code: str,
        *,
        status: str,
        actual: Any = None,
        op: str | None = None,
        threshold: Any = None,
        margin: Any = None,
    ) -> None:
        gates.append(
            {
                "code": code,
                "status": status,
                "actual": actual,
                "op": op,
                "threshold": threshold,
                "margin": margin,
            }
        )

    # Map primary readiness failure to which gate failed; earlier passed, later not_evaluated.
    reason_to_gate = {
        ZGapReason.TIME_NOT_READY: "TIME_READY",
        ZGapReason.PTB_NOT_USABLE: "PTB_USABLE",
        ZGapReason.JUMP_GUARD: "JUMP_GUARD",
        ZGapReason.MODEL_NOT_READY: "MODEL_READY",
        ZGapReason.BASIS_REJECT: "BASIS",
        ZGapReason.TAU_OUT_OF_BAND: "TAU_IN_BAND",
        ZGapReason.ABS_Z_BLOCK: "ABS_Z_BLOCK",
        ZGapReason.Z_OUT_OF_BAND: "Z_IN_BAND",
    }
    fail_gate = reason_to_gate.get(failed_reason) if failed_reason else None
    if reached_selection:
        fail_gate = None

    stopped = False
    for code in _READINESS_GATE_ORDER:
        if stopped:
            add(code, status="not_evaluated")
            continue
        if fail_gate is not None and code == fail_gate:
            add(
                code,
                status="failed",
                actual=_readiness_actual(code, decision_input, config),
                op=_readiness_op(code),
                threshold=_readiness_threshold(code, config),
            )
            stopped = True
            continue
        if fail_gate is not None and _READINESS_GATE_ORDER.index(code) > _READINESS_GATE_ORDER.index(
            fail_gate
        ):
            add(code, status="not_evaluated")
            continue
        # Passed (or readiness succeeded entirely)
        if fail_gate is None or _READINESS_GATE_ORDER.index(code) < _READINESS_GATE_ORDER.index(
            fail_gate
        ):
            add(
                code,
                status="passed",
                actual=_readiness_actual(code, decision_input, config),
                op=_readiness_op(code),
                threshold=_readiness_threshold(code, config),
            )

    # Selection gates
    if not reached_selection:
        for code in _SELECTION_GATE_ORDER:
            add(code, status="not_evaluated")
        return gates

    # Book/fee readiness from valuations
    books_ready = True
    if up_val is not None and down_val is not None:
        books_ready = bool(up_val.ready or down_val.ready)
    book_block_reasons = {
        ZGapReason.BOOK_NOT_READY,
        ZGapReason.BOOK_UNAVAILABLE,
        ZGapReason.BOOK_SYNCING,
        ZGapReason.BOOK_STALE,
        ZGapReason.BOOK_DESYNCED,
        ZGapReason.VENUE_ASK_EXPLICITLY_EMPTY,
        ZGapReason.VENUE_BID_EXPLICITLY_EMPTY,
        ZGapReason.INSUFFICIENT_DEPTH,
        ZGapReason.FEE_NOT_READY,
        ZGapReason.FEE_UNAVAILABLE,
        ZGapReason.LEG_NOT_READY,
    }
    if not books_ready or (
        selection is not None and selection.reason_code in book_block_reasons
    ):
        add("BOOK_FEE_READY", status="failed")
        add("EDGE_VS_THETA", status="not_evaluated")
        return gates
    add("BOOK_FEE_READY", status="passed")

    threshold = None
    if config is not None:
        threshold = (
            str(config.entry.theta_take)
            if config.entry.require_repricing_edge
            else "0"
        )
    edge_failed = selection is not None and selection.reason_code in {
        ZGapReason.NO_ECONOMIC_EDGE,
        ZGapReason.BELOW_THRESHOLD,
        ZGapReason.TIE_AMBIGUOUS,
        ZGapReason.BOTH_LEGS_EDGE,
    }
    best_edge = None if selection is None or selection.edge is None else str(selection.edge)
    margin = None
    if selection is not None and selection.edge is not None and config is not None:
        req = (
            config.entry.theta_take
            if config.entry.require_repricing_edge
            else Decimal("0")
        )
        margin = float(selection.edge - req)
    add(
        "EDGE_VS_THETA",
        status="failed" if edge_failed and decision.action.value != "ENTER" else "passed",
        actual=best_edge,
        op=">=",
        threshold=threshold,
        margin=margin,
    )
    return gates


def build_closest_candidate_fields(
    *,
    decision_input: ZGapDecisionSnapshot,
    decision: StrategyDecision,
    up_val: EntryLegValuation | None,
    down_val: EntryLegValuation | None,
    selection: LegSelectionResult | None,
    readiness: ReadinessResult | None,
    config: ZGapConfig | None,
) -> dict[str, Any]:
    reached = bool(readiness is not None and readiness.ready)
    # Edge evaluation reached only after readiness.
    if not reached:
        return {
            "reached_edge_evaluation": False,
            "evaluation_id": decision_input.epoch.epoch_id,
            "primary_reason": decision.reason_code,
        }
    require_repricing = True if config is None else config.entry.require_repricing_edge
    req = Decimal("0") if config is None else (
        config.entry.theta_take if require_repricing else Decimal("0")
    )

    def _edge(v: EntryLegValuation | None) -> Decimal | None:
        if v is None or not v.ready:
            return None
        return v.e_repricing if require_repricing else v.e_settlement

    e_up = _edge(up_val)
    e_down = _edge(down_val)
    candidates = [(leg, e) for leg, e in (("UP", e_up), ("DOWN", e_down)) if e is not None]
    if not candidates:
        return {
            "reached_edge_evaluation": True,
            "evaluation_id": decision_input.epoch.epoch_id,
            "primary_reason": decision.reason_code,
            "signed_margin": None,
            "selected_leg": decision.evidence.get("selected_leg"),
        }
    best_leg, best_edge = max(candidates, key=lambda item: item[1])
    margin = float(best_edge - req)
    return {
        "reached_edge_evaluation": True,
        "evaluation_id": decision_input.epoch.epoch_id,
        "selected_leg": best_leg
        if decision.evidence.get("selected_leg") is None
        else decision.evidence.get("selected_leg"),
        "executable_net_edge": str(best_edge),
        "required_edge": str(req),
        "signed_margin": margin,
        "primary_reason": decision.reason_code,
        "action": decision.action.value,
        "diagnostics": {
            "e_up": None if e_up is None else str(e_up),
            "e_down": None if e_down is None else str(e_down),
            "z": model_z(decision_input),
            "tau_s": decision_input.model.tau_s,
        },
    }


def model_z(decision_input: ZGapDecisionSnapshot) -> float | None:
    return decision_input.model.z


def _readiness_actual(
    code: str, decision_input: ZGapDecisionSnapshot, config: ZGapConfig | None
) -> Any:
    model = decision_input.model
    if code == "TIME_READY":
        return decision_input.time.ready
    if code == "PTB_USABLE":
        return None if decision_input.ptb is None else decision_input.ptb.usable_for_entry
    if code == "PTB_LAG":
        return None if decision_input.ptb is None else decision_input.ptb.boundary_lag_ms
    if code == "JUMP_GUARD":
        return model.jump_guard_tripped
    if code == "MODEL_READY":
        return model.ready
    if code == "BASIS":
        return None if model.basis_bps is None else str(model.basis_bps)
    if code == "TAU_IN_BAND":
        return model.tau_s
    if code in {"ABS_Z_BLOCK", "Z_IN_BAND"}:
        return model.z
    return None


def _readiness_op(code: str) -> str | None:
    return {
        "TIME_READY": "is_true",
        "PTB_USABLE": "is_true",
        "PTB_LAG": "<=",
        "JUMP_GUARD": "is_false",
        "MODEL_READY": "is_true",
        "BASIS": "abs_le",
        "TAU_IN_BAND": "in_range",
        "ABS_Z_BLOCK": "<",
        "Z_IN_BAND": "in_range",
    }.get(code)


def _readiness_threshold(code: str, config: ZGapConfig | None) -> Any:
    if config is None:
        return None
    if code == "PTB_LAG":
        return config.ptb_time_quality.max_ptb_lag_ms
    if code == "BASIS":
        return str(config.ptb_time_quality.basis_max_bps)
    if code == "TAU_IN_BAND":
        return {"min": config.entry.tau_min_s, "max": config.entry.tau_max_s}
    if code == "ABS_Z_BLOCK":
        return str(config.entry.block_abs_z)
    if code == "Z_IN_BAND":
        return {"min": str(config.entry.z_min), "max": str(config.entry.z_max)}
    return None
