"""Phase 1 survival — survivor damage control for paired-binary (default disabled).

Simplified live path: hard floor (advisory) → recovery → trailing → enforce dispatch.
Legacy modules (target_policy, stall, reachability) remain for earlier milestones.
See Docs/modules/survival/README.md.
"""

from tyrex_pm.survival.advisory import evaluate_survival_advisory
from tyrex_pm.survival.kill_switches import (
    ACTION_DENY_ENTRY,
    ACTION_FORCE_FLATTEN_PAIR,
    ACTION_HARD_STOP,
    ACTION_PAUSE_STRATEGY,
    KillSwitchManager,
)
from tyrex_pm.survival.context import (
    apply_survival_target_plan,
    build_survivor_leg_context,
    select_and_apply_survival_target,
)
from tyrex_pm.survival.economics import ExitEconomics
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner, planner_from_app
from tyrex_pm.survival.facts import (
    build_survival_evidence_payload,
    exit_evaluation_payload,
    target_plan_payload,
)
from tyrex_pm.survival.models import (
    EconomicsContext,
    EconomicsEvaluation,
    EconomicsVerdict,
    ExecutableExitEvidence,
    ReachabilityResult,
    ReachabilityVerdict,
    StallAction,
    StallEvaluation,
    SurvivalAdvisoryResult,
    SurvivalExitEvaluation,
    SurvivalExitVerdict,
    SurvivorLegContext,
    SurvivorLegState,
    SurvivorTargetClassification,
    SurvivorTargetMode,
    SurvivorTargetPlan,
    TrailingStopEvaluation,
    TrailingStopRuntime,
    TrailingStopState,
)
from tyrex_pm.survival.reachability import TargetReachabilityScorer
from tyrex_pm.survival.stall_exit import SurvivorStallDetector
from tyrex_pm.survival.target_policy import SurvivorTargetPolicy, TargetSelectionResult
from tyrex_pm.survival.trailing_stop import SurvivorTrailingStop

__all__ = [
    "ACTION_DENY_ENTRY",
    "ACTION_FORCE_FLATTEN_PAIR",
    "ACTION_HARD_STOP",
    "ACTION_PAUSE_STRATEGY",
    "EconomicsContext",
    "EconomicsEvaluation",
    "EconomicsVerdict",
    "ExecutableExitEvidence",
    "ExitEconomics",
    "ReachabilityResult",
    "ReachabilityVerdict",
    "StallAction",
    "StallEvaluation",
    "KillSwitchManager",
    "SurvivalAdvisoryResult",
    "SurvivalExitEvaluation",
    "SurvivalExitPlanner",
    "SurvivalExitVerdict",
    "SurvivorLegContext",
    "SurvivorLegState",
    "SurvivorStallDetector",
    "SurvivorTargetClassification",
    "SurvivorTargetMode",
    "SurvivorTargetPlan",
    "SurvivorTargetPolicy",
    "SurvivorTrailingStop",
    "TargetReachabilityScorer",
    "TargetSelectionResult",
    "TrailingStopEvaluation",
    "TrailingStopRuntime",
    "TrailingStopState",
    "apply_survival_target_plan",
    "build_survival_evidence_payload",
    "build_survivor_leg_context",
    "evaluate_survival_advisory",
    "exit_evaluation_payload",
    "planner_from_app",
    "select_and_apply_survival_target",
    "target_plan_payload",
]
