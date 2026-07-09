"""Immediate survivor hard floor (Phase 1 simplified protection)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tyrex_pm.runtime.config import SurvivorFloorConfig
from tyrex_pm.survival.enforcement import is_enforce
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import SurvivalExitEvaluation


@dataclass(frozen=True)
class SurvivorFloorEvaluation:
    floor_price: Decimal
    should_exit: bool
    reason: str | None
    evidence: dict[str, Any]
    floor_set: bool = False


class SurvivorHardFloor:
    def compute_floor_price(
        self,
        *,
        survivor_entry: Decimal,
        cfg: SurvivorFloorConfig,
    ) -> Decimal:
        if cfg.mode == "winner_entry_price":
            return survivor_entry + cfg.floor_buffer
        return survivor_entry + cfg.floor_buffer

    def evaluate(
        self,
        *,
        cfg: SurvivorFloorConfig,
        exit_eval: SurvivalExitEvaluation,
        survivor_entry: Decimal,
        stored_floor: Decimal | None,
        planner: SurvivalExitPlanner | None = None,
    ) -> SurvivorFloorEvaluation:
        if not cfg.enabled:
            return SurvivorFloorEvaluation(
                floor_price=survivor_entry,
                should_exit=False,
                reason=None,
                evidence={"enabled": False},
            )

        floor = stored_floor if stored_floor is not None else self.compute_floor_price(
            survivor_entry=survivor_entry, cfg=cfg
        )
        bid = self._executable_bid(exit_eval, planner)
        ev = exit_eval.evidence

        if bid is None:
            return SurvivorFloorEvaluation(
                floor_price=floor,
                should_exit=False,
                reason="no_executable_bid",
                evidence={"floor_price": str(floor), "quality_verdict": ev.quality_verdict},
            )

        gates_ok = (
            ev.available_depth_fraction >= cfg.min_depth_fraction
            and (ev.spread is None or ev.spread <= cfg.max_spread)
        )
        if cfg.require_fresh_book and ev.quality_verdict in {"reject_decision"}:
            gates_ok = False

        triggered = gates_ok and bid <= floor
        should_exit = triggered and is_enforce(cfg.enforcement_mode)
        if triggered and not should_exit:
            reason = "advisory_only"
        elif triggered:
            reason = "hard_floor_breach"
        else:
            reason = None

        evidence: dict[str, Any] = {
            "survivor_entry_price": str(survivor_entry),
            "floor_price": str(floor),
            "current_executable_bid": str(bid),
            "touch_bid": str(ev.touch_bid) if ev.touch_bid is not None else None,
            "enforcement_mode": cfg.enforcement_mode,
            "depth_fraction": str(ev.available_depth_fraction),
        }
        return SurvivorFloorEvaluation(
            floor_price=floor,
            should_exit=should_exit,
            reason=reason,
            evidence=evidence,
        )

    @staticmethod
    def _executable_bid(
        exit_eval: SurvivalExitEvaluation,
        planner: SurvivalExitPlanner | None,
    ) -> Decimal | None:
        if planner is not None:
            bid = planner.executable_bid_for_progress(exit_eval)
            if bid is not None:
                return bid
        ev = exit_eval.evidence
        return ev.sweep_vwap or ev.executable_bid or ev.touch_bid
