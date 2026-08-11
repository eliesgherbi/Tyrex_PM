"""Pure Z-Gap decision policies (no host/OMS/risk engine).

Consumes immutable snapshots/valuations + normalized flags.
Emits strategy-level economic desire and stable reason codes.
Emits preference only; HoldToResolutionIntent is created by the thin strategy (F5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.indicators.reference_basis import BasisResult, BasisValidity
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.z_gap.config import ZGapConfig
from tyrex_pm.strategies.z_gap.reasons import ZGapReason
from tyrex_pm.strategies.z_gap.snapshots import ZGapModelSnapshot
from tyrex_pm.strategies.z_gap.state import ThesisConfirmPhase, ThesisConfirmState
from tyrex_pm.strategies.z_gap.valuations import (
    EntryLegValuation,
    PositionValuation,
    ZGapLeg,
)


class ResolutionPreference(str, Enum):
    """Z-Gap-local semantic result only — not a framework intent."""

    SELL = "SELL"
    CONTINUE = "CONTINUE"
    HOLD_RESOLUTION = "HOLD_RESOLUTION"


@dataclass(frozen=True, kw_only=True)
class NormalizedRiskFlags:
    """Host/risk-normalized flags — F2 does not evaluate risk itself."""

    unknown_inventory: bool = False
    emergency: bool = False
    kill_switch: bool = False


@dataclass(frozen=True, kw_only=True)
class ReadinessResult:
    ready: bool
    action: StrategyAction
    reason_code: ZGapReason
    evidence: Mapping[str, Any] = field(default_factory=dict)
    blockers: tuple[ZGapReason, ...] = ()


@dataclass(frozen=True, kw_only=True)
class LegSelectionResult:
    selected: ZGapLeg | None
    edge: Decimal | None
    reason_code: ZGapReason
    both_legs_edge: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ThesisPolicyResult:
    valid: bool
    confirming: bool
    invalidated: bool
    state: ThesisConfirmState
    reason_code: ZGapReason
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class RealizationResult:
    exit_rich: bool
    reason_code: ZGapReason
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class TimeResolutionResult:
    preference: ResolutionPreference
    reason_code: ZGapReason
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class PolicyDecision:
    action: StrategyAction
    reason_code: ZGapReason
    selected_leg: ZGapLeg | None = None
    resolution_preference: ResolutionPreference | None = None
    thesis_state: ThesisConfirmState | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


def evaluate_readiness(
    *,
    model: ZGapModelSnapshot,
    ptb: PtbSnapshot | None,
    basis: BasisResult | None,
    time_ready: bool,
    config: ZGapConfig,
    time_evidence: Mapping[str, Any] | None = None,
) -> ReadinessResult:
    # Trading still uses a deterministic primary blocker, but diagnostics must
    # retain every independently observable failure.  Returning at the first
    # failed gate made live reports hide the next problem (for example a late
    # PTB behind an uncertain clock), forcing one-live-run-at-a-time debugging.
    failures: list[tuple[ZGapReason, StrategyAction, dict[str, Any]]] = []
    if not time_ready:
        failures.append(
            (
                ZGapReason.TIME_NOT_READY,
                StrategyAction.WAIT,
                {"time": dict(time_evidence or {})},
            )
        )

    ptb_evidence = {
        "quality": None if ptb is None else ptb.quality.value,
        "boundary_lag_ms": None if ptb is None else ptb.boundary_lag_ms,
        "maximum_boundary_lag_ms": config.ptb_time_quality.max_ptb_lag_ms,
        "readiness_reasons": [] if ptb is None else list(ptb.readiness_reasons),
    }
    if ptb is None or not ptb.usable_for_entry:
        failures.append(
            (ZGapReason.PTB_NOT_USABLE, StrategyAction.WAIT, {"ptb": ptb_evidence})
        )
    elif (
        ptb.boundary_lag_ms is not None
        and ptb.boundary_lag_ms > config.ptb_time_quality.max_ptb_lag_ms
    ):
        failures.append(
            (ZGapReason.PTB_NOT_USABLE, StrategyAction.SKIP, {"ptb": ptb_evidence})
        )

    if model.jump_guard_tripped:
        failures.append((ZGapReason.JUMP_GUARD, StrategyAction.WAIT, {}))
    if not model.ready or model.tau_s is None:
        failures.append(
            (
                ZGapReason.MODEL_NOT_READY,
                StrategyAction.WAIT,
                {"model_reject_reasons": list(model.reject_reasons)},
            )
        )

    if basis is not None:
        if basis.validity is not BasisValidity.VALID or basis.basis_bps is None:
            failures.append(
                (
                    ZGapReason.BASIS_REJECT,
                    StrategyAction.WAIT,
                    {"basis_validity": basis.validity.value},
                )
            )
        elif abs(basis.basis_bps) > config.ptb_time_quality.basis_max_bps:
            failures.append(
                (
                    ZGapReason.BASIS_REJECT,
                    StrategyAction.SKIP,
                    {"basis_bps": str(basis.basis_bps)},
                )
            )

    if model.tau_s is not None and not (
        config.entry.tau_min_s <= model.tau_s <= config.entry.tau_max_s
    ):
        failures.append(
            (ZGapReason.TAU_OUT_OF_BAND, StrategyAction.SKIP, {"tau_s": model.tau_s})
        )
    if model.z is not None:
        abs_z = abs(model.z)
        if abs_z >= float(config.entry.block_abs_z):
            failures.append((ZGapReason.ABS_Z_BLOCK, StrategyAction.SKIP, {"z": model.z}))
        elif not (float(config.entry.z_min) <= abs_z <= float(config.entry.z_max)):
            failures.append((ZGapReason.Z_OUT_OF_BAND, StrategyAction.SKIP, {"z": model.z}))

    if failures:
        primary_reason, primary_action, _ = failures[0]
        merged: dict[str, Any] = {
            "strategy_inputs_eligible": False,
            "blockers": [reason.value for reason, _, _ in failures],
        }
        for _, _, item in failures:
            merged.update(item)
        return ReadinessResult(
            ready=False,
            action=primary_action,
            reason_code=primary_reason,
            evidence=merged,
            blockers=tuple(reason for reason, _, _ in failures),
        )
    return ReadinessResult(
        ready=True,
        action=StrategyAction.ENTER,  # candidate path; selection may still skip
        reason_code=ZGapReason.MODEL_READY,
        evidence={
            "strategy_inputs_eligible": True,
            "blockers": [],
            "time": dict(time_evidence or {}),
            "ptb": ptb_evidence,
        },
    )


def select_leg(
    up: EntryLegValuation,
    down: EntryLegValuation,
    *,
    config: ZGapConfig,
) -> LegSelectionResult:
    """argmax valid positive economic edge; no sign(z) restriction."""
    threshold = config.entry.theta_take if config.entry.require_repricing_edge else Decimal("0")
    eps = config.entry.tie_epsilon

    def _edge(v: EntryLegValuation) -> Decimal | None:
        if not v.ready:
            return None
        e = v.e_repricing if config.entry.require_repricing_edge else v.e_settlement
        return e

    e_up = _edge(up)
    e_down = _edge(down)
    evidence = {
        "e_up": None if e_up is None else str(e_up),
        "e_down": None if e_down is None else str(e_down),
        "p_up": None if up.fair_probability is None else str(up.fair_probability),
        "p_down": None if down.fair_probability is None else str(down.fair_probability),
    }

    valid: list[tuple[ZGapLeg, Decimal]] = []
    for leg, e in ((ZGapLeg.UP, e_up), (ZGapLeg.DOWN, e_down)):
        if e is not None and e > 0 and e >= threshold:
            valid.append((leg, e))

    if len(valid) == 0:
        # Distinguish no positive edge vs below threshold
        positives = [e for e in (e_up, e_down) if e is not None and e > 0]
        if positives:
            return LegSelectionResult(
                selected=None,
                edge=None,
                reason_code=ZGapReason.BELOW_THRESHOLD,
                evidence=evidence,
            )
        return LegSelectionResult(
            selected=None,
            edge=None,
            reason_code=ZGapReason.NO_ECONOMIC_EDGE,
            evidence=evidence,
        )

    if len(valid) == 2 and config.entry.reject_both_legs_edge:
        return LegSelectionResult(
            selected=None,
            edge=None,
            reason_code=ZGapReason.BOTH_LEGS_EDGE,
            both_legs_edge=True,
            evidence=evidence,
        )

    if len(valid) == 2:
        a, b = valid[0], valid[1]
        if abs(a[1] - b[1]) <= eps:
            return LegSelectionResult(
                selected=None,
                edge=None,
                reason_code=ZGapReason.TIE_AMBIGUOUS,
                both_legs_edge=True,
                evidence=evidence,
            )

    best_leg, best_edge = max(valid, key=lambda item: item[1])
    if len(valid) == 1 and eps >= 0:
        # Near-tie against the non-selected positive-but-subthreshold peer is N/A
        pass
    # Near-tie when both positive but only one above threshold: still select winner
    if e_up is not None and e_down is not None and e_up > 0 and e_down > 0:
        if abs(e_up - e_down) <= eps and not config.entry.reject_both_legs_edge:
            return LegSelectionResult(
                selected=None,
                edge=None,
                reason_code=ZGapReason.TIE_AMBIGUOUS,
                both_legs_edge=True,
                evidence=evidence,
            )

    return LegSelectionResult(
        selected=best_leg,
        edge=best_edge,
        reason_code=ZGapReason.ENTRY_CANDIDATE,
        both_legs_edge=False,
        evidence=evidence,
    )


def evaluate_thesis(
    *,
    p_held: float | None,
    model_valid: bool,
    model_fresh: bool,
    now_mono_ns: int,
    prior: ThesisConfirmState,
    config: ZGapConfig,
) -> ThesisPolicyResult:
    """Invalidate when p_held stays below p_stop for stop_confirm_s (monotonic)."""
    if not model_valid or not model_fresh:
        if config.thesis.reset_on_stale_model:
            return ThesisPolicyResult(
                valid=False,
                confirming=False,
                invalidated=True,
                state=ThesisConfirmState(),
                reason_code=ZGapReason.THESIS_RESET_STALE,
            )
        return ThesisPolicyResult(
            valid=False,
            confirming=False,
            invalidated=True,
            state=prior,
            reason_code=ZGapReason.THESIS_INVALID,
        )

    if p_held is None:
        return ThesisPolicyResult(
            valid=False,
            confirming=False,
            invalidated=True,
            state=ThesisConfirmState(),
            reason_code=ZGapReason.MODEL_NOT_READY,
        )

    p_stop = float(config.thesis.p_stop)
    confirm_ns = int(config.thesis.stop_confirm_s * 1_000_000_000)

    if p_held >= p_stop:
        return ThesisPolicyResult(
            valid=True,
            confirming=False,
            invalidated=False,
            state=ThesisConfirmState(
                phase=ThesisConfirmPhase.IDLE,
                adverse_since_mono_ns=None,
                last_p_held=p_held,
            ),
            reason_code=ZGapReason.THESIS_VALID,
            evidence={"p_held": p_held, "p_stop": p_stop},
        )

    # Adverse
    since = prior.adverse_since_mono_ns
    if since is None or prior.phase is ThesisConfirmPhase.IDLE:
        since = now_mono_ns
        return ThesisPolicyResult(
            valid=True,
            confirming=True,
            invalidated=False,
            state=ThesisConfirmState(
                phase=ThesisConfirmPhase.CONFIRMING,
                adverse_since_mono_ns=since,
                last_p_held=p_held,
            ),
            reason_code=ZGapReason.THESIS_CONFIRMING,
            evidence={"p_held": p_held, "p_stop": p_stop},
        )

    elapsed = now_mono_ns - since
    if elapsed < 0:
        # Monotonic regression — reset confirmation
        return ThesisPolicyResult(
            valid=True,
            confirming=True,
            invalidated=False,
            state=ThesisConfirmState(
                phase=ThesisConfirmPhase.CONFIRMING,
                adverse_since_mono_ns=now_mono_ns,
                last_p_held=p_held,
            ),
            reason_code=ZGapReason.THESIS_CONFIRMING,
            evidence={"p_held": p_held, "mono_reset": True},
        )

    if elapsed >= confirm_ns:
        return ThesisPolicyResult(
            valid=False,
            confirming=False,
            invalidated=True,
            state=ThesisConfirmState(
                phase=ThesisConfirmPhase.INVALIDATED,
                adverse_since_mono_ns=since,
                last_p_held=p_held,
            ),
            reason_code=ZGapReason.THESIS_INVALID,
            evidence={"p_held": p_held, "confirm_ns": confirm_ns, "elapsed_ns": elapsed},
        )

    return ThesisPolicyResult(
        valid=True,
        confirming=True,
        invalidated=False,
        state=ThesisConfirmState(
            phase=ThesisConfirmPhase.CONFIRMING,
            adverse_since_mono_ns=since,
            last_p_held=p_held,
        ),
        reason_code=ZGapReason.THESIS_CONFIRMING,
        evidence={"p_held": p_held, "elapsed_ns": elapsed},
    )


def evaluate_realization(
    position: PositionValuation,
    *,
    config: ZGapConfig,
) -> RealizationResult:
    if not position.ready or position.market_richness is None:
        return RealizationResult(
            exit_rich=False,
            reason_code=ZGapReason.BOOK_NOT_READY,
        )
    if (
        position.bid_depth is not None
        and config.realization.min_exit_depth > 0
        and position.bid_depth < config.realization.min_exit_depth
    ):
        return RealizationResult(
            exit_rich=False,
            reason_code=ZGapReason.BOOK_NOT_READY,
            evidence={"bid_depth": str(position.bid_depth)},
        )
    if position.market_richness >= config.realization.theta_rich:
        return RealizationResult(
            exit_rich=True,
            reason_code=ZGapReason.MARKET_RICH_EXIT,
            evidence={
                "market_richness": str(position.market_richness),
                "theta_rich": str(config.realization.theta_rich),
            },
        )
    return RealizationResult(
        exit_rich=False,
        reason_code=ZGapReason.CONTINUE_HOLD,
        evidence={"market_richness": str(position.market_richness)},
    )


def evaluate_time_resolution(
    *,
    tau_s: float | None,
    resolution_capability: bool,
    v_sell: Decimal | None,
    v_resolve_adj: Decimal | None,
    config: ZGapConfig,
) -> TimeResolutionResult:
    """Semantic preference among SELL / CONTINUE / HOLD_RESOLUTION."""
    # Capability is an explicit input (composition-supplied; default unavailable).
    capable = bool(resolution_capability)
    if not capable:
        capable = bool(config.time_resolution.resolution_capability_default)

    # Pre-resolution operational flatten: only when resolution hold is unavailable.
    if (
        tau_s is not None
        and tau_s <= config.time_resolution.flatten_before_event_end_s
        and not capable
    ):
        return TimeResolutionResult(
            preference=ResolutionPreference.SELL,
            reason_code=ZGapReason.TIME_SELL,
            evidence={"tau_s": tau_s, "capable": False},
        )

    if capable and v_sell is not None and v_resolve_adj is not None:
        margin = config.time_resolution.sell_vs_resolve_margin
        if v_sell > v_resolve_adj + margin:
            return TimeResolutionResult(
                preference=ResolutionPreference.SELL,
                reason_code=ZGapReason.SELL_BEATS_RESOLUTION,
                evidence={
                    "v_sell": str(v_sell),
                    "v_resolve_adj": str(v_resolve_adj),
                },
            )
        if v_resolve_adj >= v_sell:
            return TimeResolutionResult(
                preference=ResolutionPreference.HOLD_RESOLUTION,
                reason_code=ZGapReason.RESOLUTION_PREFERENCE,
                evidence={
                    "v_sell": str(v_sell),
                    "v_resolve_adj": str(v_resolve_adj),
                },
            )

    return TimeResolutionResult(
        preference=ResolutionPreference.CONTINUE,
        reason_code=ZGapReason.CONTINUE_HOLD,
        evidence={"resolution_capability": capable},
    )


def combine_precedence(
    *,
    flags: NormalizedRiskFlags,
    model_valid: bool,
    thesis: ThesisPolicyResult | None,
    realization: RealizationResult | None,
    time_res: TimeResolutionResult | None,
    entry_selection: LegSelectionResult | None = None,
    flat: bool = True,
) -> PolicyDecision:
    """
    L1 UNKNOWN → block
    L2 Emergency
    L3 Model validity
    L4 Thesis invalidation
    L5 Economic realization / rich exit
    L6 Time / resolution preference
    L7 Hold
    """
    if flags.unknown_inventory:
        return PolicyDecision(
            action=StrategyAction.BLOCKED,
            reason_code=ZGapReason.UNKNOWN_STATE,
            evidence={"layer": 1},
        )
    if flags.emergency or flags.kill_switch:
        return PolicyDecision(
            action=StrategyAction.FLATTEN,
            reason_code=ZGapReason.EMERGENCY_FLAG,
            evidence={"layer": 2, "kill_switch": flags.kill_switch},
        )
    if not model_valid:
        if flat:
            return PolicyDecision(
                action=StrategyAction.WAIT,
                reason_code=ZGapReason.MODEL_NOT_READY,
                evidence={"layer": 3},
            )
        return PolicyDecision(
            action=StrategyAction.FLATTEN,
            reason_code=ZGapReason.MODEL_NOT_READY,
            evidence={"layer": 3, "active": True},
        )
    if not flat and thesis is not None and thesis.invalidated:
        return PolicyDecision(
            action=StrategyAction.EXIT,
            reason_code=ZGapReason.THESIS_INVALID,
            thesis_state=thesis.state,
            evidence={"layer": 4},
        )
    if not flat and realization is not None and realization.exit_rich:
        return PolicyDecision(
            action=StrategyAction.EXIT,
            reason_code=ZGapReason.MARKET_RICH_EXIT,
            evidence={"layer": 5, **dict(realization.evidence)},
        )
    if not flat and time_res is not None:
        if time_res.preference is ResolutionPreference.SELL:
            return PolicyDecision(
                action=StrategyAction.EXIT,
                reason_code=time_res.reason_code,
                resolution_preference=time_res.preference,
                evidence={"layer": 6, **dict(time_res.evidence)},
            )
        if time_res.preference is ResolutionPreference.HOLD_RESOLUTION:
            # Generic action remains HOLD; strategy may emit HoldToResolutionIntent.
            return PolicyDecision(
                action=StrategyAction.HOLD,
                reason_code=ZGapReason.RESOLUTION_PREFERENCE,
                resolution_preference=time_res.preference,
                thesis_state=None if thesis is None else thesis.state,
                evidence={"layer": 6, "emit_hold_to_resolution_intent": True},
            )

    if flat and entry_selection is not None:
        if entry_selection.selected is not None:
            return PolicyDecision(
                action=StrategyAction.ENTER,
                reason_code=ZGapReason.ENTRY_CANDIDATE,
                selected_leg=entry_selection.selected,
                evidence={"layer": "entry", **dict(entry_selection.evidence)},
            )
        return PolicyDecision(
            action=StrategyAction.SKIP,
            reason_code=entry_selection.reason_code,
            evidence={"layer": "entry", **dict(entry_selection.evidence)},
        )

    if not flat:
        return PolicyDecision(
            action=StrategyAction.HOLD,
            reason_code=ZGapReason.CONTINUE_HOLD,
            thesis_state=None if thesis is None else thesis.state,
            evidence={"layer": 7},
        )

    return PolicyDecision(
        action=StrategyAction.SKIP,
        reason_code=ZGapReason.SKIP,
        evidence={"layer": 7},
    )
