"""Phase 1 survival domain models (M1/M2)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Literal


class SurvivorTargetMode(str, Enum):
    FULL_RECOVERY = "full_recovery"
    BREAKEVEN = "breakeven"
    SMALL_PROFIT = "small_profit"
    SMALL_LOSS = "small_loss"
    DYNAMIC = "dynamic"


class SurvivorTargetClassification(str, Enum):
    IMPOSSIBLE = "impossible"
    UNREALISTIC = "unrealistic"
    VALID_CANDIDATE = "valid_candidate"


@dataclass(frozen=True)
class SurvivorLegContext:
    survivor_leg: Literal["yes", "no"]
    yes_entry_qty: Decimal
    no_entry_qty: Decimal
    yes_entry_cash: Decimal
    no_entry_cash: Decimal
    loser_exit_qty: Decimal
    loser_exit_cash: Decimal
    survivor_remaining_qty: Decimal
    estimated_fees: Decimal
    slippage_buffer: Decimal
    desired_net_profit_total: Decimal
    seconds_to_close: float | None
    loser_exit_ts: float
    ledger_source: str = "matched_cash"


@dataclass(frozen=True)
class SurvivorLegState:
    survivor_bid_0: Decimal | None
    selected_target: Decimal
    selected_mode: SurvivorTargetMode
    loser_exit_ts: float
    seconds_to_close_0: float | None
    available_survival_time: float | None


@dataclass(frozen=True)
class SurvivorTargetPlan:
    mode: SurvivorTargetMode
    classification: SurvivorTargetClassification
    target_total_net: Decimal
    required_survivor_exit_price: Decimal
    trigger_target: Decimal
    total_entry_cost: Decimal
    loser_exit_proceeds: Decimal
    evidence: dict[str, str]


@dataclass(frozen=True)
class ExecutableExitEvidence:
    touch_bid: Decimal | None
    executable_bid: Decimal | None
    sweep_vwap: Decimal | None
    worst_price_to_fill: Decimal | None
    available_depth: Decimal
    available_depth_fraction: Decimal
    expected_slippage: Decimal | None
    book_age_ms: int | None
    snapshot_id: str
    quality_verdict: str
    spread: Decimal | None
    planner_evidence_ref: dict[str, Any] | None
    quality_reject_detail: dict[str, bool] | None = None


SurvivalExitVerdict = Literal["proceed_full", "proceed_partial", "defer", "blocked"]


@dataclass(frozen=True)
class SurvivalExitEvaluation:
    verdict: SurvivalExitVerdict
    recommended_qty: Decimal
    evidence: ExecutableExitEvidence
    reason: str | None


class ReachabilityVerdict(str, Enum):
    REACHABLE = "reachable"
    WEAK = "weak"
    UNLIKELY = "unlikely"


@dataclass(frozen=True)
class ReachabilityResult:
    verdict: ReachabilityVerdict
    score: Decimal
    distance_to_target: Decimal
    evidence: dict[str, Any]


class StallAction(str, Enum):
    DOWNGRADE = "downgrade"
    EXIT_SMALL_LOSS = "exit_small_loss"
    EXIT_MARKET = "exit_market"


@dataclass(frozen=True)
class StallEvaluation:
    stalled: bool
    progress_to_target: Decimal
    elapsed_fraction: Decimal
    action: StallAction | None
    evidence: dict[str, Any]


class TrailingStopState(str, Enum):
    DISARMED = "disarmed"
    ARMED = "armed"
    TRIGGERED = "triggered"
    EXIT_PENDING = "exit_pending"


@dataclass
class TrailingStopRuntime:
    state: TrailingStopState = TrailingStopState.DISARMED
    peak_executable_bid: Decimal | None = None
    trail_floor: Decimal | None = None
    armed_at_ts: float | None = None


@dataclass(frozen=True)
class TrailingStopEvaluation:
    new_runtime: TrailingStopRuntime
    should_exit: bool
    reason: str | None
    evidence: dict[str, Any]
    armed_transition: bool = False
    triggered_transition: bool = False


class EconomicsVerdict(str, Enum):
    PROCEED = "proceed"
    DEFER = "defer"
    ABORT_ENTRY = "abort_entry"
    EXIT_SURVIVOR_EARLY = "exit_survivor_early"


@dataclass(frozen=True)
class EconomicsContext:
    phase: Literal["pre_entry", "survivor_hold", "pre_close"]
    total_entry_cost: Decimal | None
    loser_exit_proceeds: Decimal | None
    survivor_qty: Decimal | None
    exit_eval: SurvivalExitEvaluation | None
    selected_target_mode: SurvivorTargetMode | None
    estimated_fees: Decimal
    slippage_buffer: Decimal
    minimum_acceptable_net: Decimal


@dataclass(frozen=True)
class EconomicsEvaluation:
    verdict: EconomicsVerdict
    expected_net_total: Decimal | None
    expected_net_per_pair: Decimal | None
    evidence: dict[str, str]


@dataclass(frozen=True)
class SurvivalAdvisoryResult:
    exit_eval: SurvivalExitEvaluation | None
    reachability: ReachabilityResult | None
    stall: StallEvaluation | None
    trailing: TrailingStopEvaluation | None
    economics: EconomicsEvaluation | None
    floor: object | None = None
    recovery: object | None = None
    enforce_exit: bool = False
    enforce_exit_reason: str | None = None
    enforce_module: str | None = None
    enforce_stall_downgrade: bool = False
