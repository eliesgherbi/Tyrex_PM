"""Ledger-based survivor target policy (Phase 1 M1)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tyrex_pm.runtime.config import SurvivalTargetPolicyConfig
from tyrex_pm.survival.models import (
    SurvivorLegContext,
    SurvivorTargetClassification,
    SurvivorTargetMode,
    SurvivorTargetPlan,
)

DYNAMIC_DOWNGRADE_CHAIN: tuple[SurvivorTargetMode, ...] = (
    SurvivorTargetMode.FULL_RECOVERY,
    SurvivorTargetMode.BREAKEVEN,
    SurvivorTargetMode.SMALL_PROFIT,
    SurvivorTargetMode.SMALL_LOSS,
)


@dataclass(frozen=True)
class TargetSelectionResult:
    plan: SurvivorTargetPlan
    downgrades: tuple[tuple[SurvivorTargetMode, SurvivorTargetMode], ...]
    failed_attempts: tuple[SurvivorTargetPlan, ...]


class SurvivorTargetPolicy:
    def __init__(self, cfg: SurvivalTargetPolicyConfig) -> None:
        self._cfg = cfg

    def compute_required_exit_price(
        self,
        *,
        target_total_net: Decimal,
        total_entry_cost: Decimal,
        loser_exit_proceeds: Decimal,
        survivor_qty: Decimal,
        estimated_fees: Decimal,
        slippage_buffer: Decimal,
    ) -> Decimal:
        if survivor_qty <= 0:
            return Decimal("0")
        numerator = (
            target_total_net
            + total_entry_cost
            - loser_exit_proceeds
            + estimated_fees
            + slippage_buffer
        )
        return numerator / survivor_qty

    def classify_price(self, required_price: Decimal) -> SurvivorTargetClassification:
        if required_price > Decimal("1"):
            return SurvivorTargetClassification.IMPOSSIBLE
        if required_price > self._cfg.max_reasonable_exit_price:
            return SurvivorTargetClassification.UNREALISTIC
        return SurvivorTargetClassification.VALID_CANDIDATE

    def _target_total_net_for_mode(
        self,
        ctx: SurvivorLegContext,
        mode: SurvivorTargetMode,
    ) -> Decimal:
        pair_qty = ctx.survivor_remaining_qty
        if mode == SurvivorTargetMode.FULL_RECOVERY:
            return ctx.desired_net_profit_total
        if mode == SurvivorTargetMode.BREAKEVEN:
            return Decimal("0")
        if mode == SurvivorTargetMode.SMALL_PROFIT:
            return self._cfg.small_profit_min_usd_per_pair * pair_qty
        if mode == SurvivorTargetMode.SMALL_LOSS:
            return -(self._cfg.small_loss_max_usd_per_pair * pair_qty)
        raise ValueError(f"unsupported mode for target_total_net: {mode}")

    def plan_for_mode(
        self,
        ctx: SurvivorLegContext,
        mode: SurvivorTargetMode,
    ) -> SurvivorTargetPlan:
        total_entry_cost = ctx.yes_entry_cash + ctx.no_entry_cash
        target_total_net = self._target_total_net_for_mode(ctx, mode)
        required = self.compute_required_exit_price(
            target_total_net=target_total_net,
            total_entry_cost=total_entry_cost,
            loser_exit_proceeds=ctx.loser_exit_cash,
            survivor_qty=ctx.survivor_remaining_qty,
            estimated_fees=ctx.estimated_fees,
            slippage_buffer=Decimal("0"),
        )
        classification = self.classify_price(required)
        trigger = required + ctx.slippage_buffer
        evidence = {
            "mode": mode.value,
            "classification": classification.value,
            "target_total_net": str(target_total_net),
            "total_entry_cost": str(total_entry_cost),
            "loser_exit_proceeds": str(ctx.loser_exit_cash),
            "survivor_qty": str(ctx.survivor_remaining_qty),
            "estimated_fees": str(ctx.estimated_fees),
            "slippage_buffer": str(ctx.slippage_buffer),
            "ledger_source": ctx.ledger_source,
        }
        return SurvivorTargetPlan(
            mode=mode,
            classification=classification,
            target_total_net=target_total_net,
            required_survivor_exit_price=required,
            trigger_target=trigger,
            total_entry_cost=total_entry_cost,
            loser_exit_proceeds=ctx.loser_exit_cash,
            evidence=evidence,
        )

    def select_plan(self, ctx: SurvivorLegContext) -> TargetSelectionResult:
        try:
            mode_cfg = SurvivorTargetMode(self._cfg.mode)
        except ValueError:
            mode_cfg = SurvivorTargetMode.DYNAMIC
        if mode_cfg != SurvivorTargetMode.DYNAMIC:
            plan = self.plan_for_mode(ctx, mode_cfg)
            return TargetSelectionResult(plan=plan, downgrades=(), failed_attempts=())

        downgrades: list[tuple[SurvivorTargetMode, SurvivorTargetMode]] = []
        failed: list[SurvivorTargetPlan] = []
        chain = DYNAMIC_DOWNGRADE_CHAIN
        final: SurvivorTargetPlan | None = None

        for idx, mode in enumerate(chain):
            plan = self.plan_for_mode(ctx, mode)
            if plan.classification == SurvivorTargetClassification.VALID_CANDIDATE:
                final = plan
                break
            failed.append(plan)
            if idx + 1 < len(chain):
                downgrades.append((mode, chain[idx + 1]))
            else:
                final = plan

        assert final is not None
        return TargetSelectionResult(
            plan=final,
            downgrades=tuple(downgrades),
            failed_attempts=tuple(failed),
        )
