"""Target reachability scoring using executable evidence (Phase 1 M3)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.runtime.config import ReachabilityConfig
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import ReachabilityResult, ReachabilityVerdict, SurvivorLegState, SurvivalExitEvaluation

_EPS = Decimal("0.0001")
_ONE = Decimal("1")


def _clamp01(value: Decimal) -> Decimal:
    if value <= 0:
        return Decimal("0")
    if value >= 1:
        return _ONE
    return value


def _quality_score(quality_verdict: str | None) -> Decimal:
    if not quality_verdict:
        return Decimal("0.5")
    v = quality_verdict.lower()
    if v == "pass":
        return _ONE
    if v in {"degraded", "emergency_only"}:
        return Decimal("0.5")
    return Decimal("0")


class TargetReachabilityScorer:
    def __init__(self, cfg: ReachabilityConfig) -> None:
        self._cfg = cfg

    def score(
        self,
        *,
        ctx: SurvivorLegState,
        target_price: Decimal,
        exit_eval: SurvivalExitEvaluation,
        timing: Any | None = None,
        quality_meta: dict[str, Any] | None = None,
        planner: SurvivalExitPlanner | None = None,
    ) -> ReachabilityResult:
        ev = exit_eval.evidence
        bid = None
        if planner is not None:
            bid = planner.executable_bid_for_progress(exit_eval)
        if bid is None:
            bid = ev.sweep_vwap or ev.executable_bid
        if bid is None:
            bid = Decimal("0")

        distance = max(target_price - bid, Decimal("0"))
        span = max(target_price - (ctx.survivor_bid_0 or Decimal("0")), _EPS)
        distance_component = _clamp01(_ONE - (distance / span))

        seconds_to_close = None
        phase = None
        if timing is not None:
            seconds_to_close = getattr(timing, "seconds_to_close", None)
            phase = getattr(timing, "phase", None)
        if seconds_to_close is None:
            seconds_to_close = ctx.seconds_to_close_0
        time_component = Decimal("0.5")
        if seconds_to_close is not None and ctx.available_survival_time:
            avail = max(Decimal(str(ctx.available_survival_time)), _EPS)
            time_component = _clamp01(Decimal(str(seconds_to_close)) / avail)

        spread = ev.spread
        spread_component = Decimal("0.5")
        if spread is not None and spread > 0:
            spread_component = _clamp01(_ONE - (spread / Decimal("0.10")))

        depth_component = _clamp01(ev.available_depth_fraction)

        velocity = Decimal("0.5")
        if quality_meta and quality_meta.get("book_velocity") is not None:
            velocity = _clamp01(Decimal(str(quality_meta["book_velocity"])))

        quality_component = _quality_score(ev.quality_verdict)

        w = self._cfg.weights
        score = (
            w.distance_to_target * distance_component
            + w.time_remaining * time_component
            + w.spread * spread_component
            + w.depth_at_size * depth_component
            + w.book_velocity * velocity
            + w.quality_stability * quality_component
        )

        if score >= self._cfg.reachable_min_score:
            verdict = ReachabilityVerdict.REACHABLE
        elif score >= self._cfg.weak_min_score:
            verdict = ReachabilityVerdict.WEAK
        else:
            verdict = ReachabilityVerdict.UNLIKELY

        evidence = {
            "score": str(score),
            "distance_to_target": str(distance),
            "executable_bid": str(bid),
            "touch_bid": str(ev.touch_bid) if ev.touch_bid is not None else None,
            "sweep_vwap": str(ev.sweep_vwap) if ev.sweep_vwap is not None else None,
            "depth_fraction": str(ev.available_depth_fraction),
            "seconds_to_close": seconds_to_close,
            "phase": phase,
            "spread": str(spread) if spread is not None else None,
            "quality_verdict": ev.quality_verdict,
            "components": {
                "distance": str(distance_component),
                "time": str(time_component),
                "spread": str(spread_component),
                "depth": str(depth_component),
                "velocity": str(velocity),
                "quality": str(quality_component),
            },
        }
        return ReachabilityResult(
            verdict=verdict,
            score=score,
            distance_to_target=distance,
            evidence=evidence,
        )
