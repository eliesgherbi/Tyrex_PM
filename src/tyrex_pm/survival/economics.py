"""Fee/slippage-aware exit economics gate (Phase 1 M5)."""

from __future__ import annotations

from decimal import Decimal

from tyrex_pm.runtime.config import EconomicsConfig
from tyrex_pm.survival.enforcement import is_enforce
from tyrex_pm.survival.exit_planning import SurvivalExitPlanner
from tyrex_pm.survival.models import EconomicsContext, EconomicsEvaluation, EconomicsVerdict, SurvivalExitEvaluation

_EPS = Decimal("0.0001")


class ExitEconomics:
    def evaluate(self, ctx: EconomicsContext, cfg: EconomicsConfig) -> EconomicsEvaluation:
        if not cfg.enabled:
            return EconomicsEvaluation(
                verdict=EconomicsVerdict.PROCEED,
                expected_net_total=None,
                expected_net_per_pair=None,
                evidence={"enabled": "false"},
            )

        qty = ctx.survivor_qty or Decimal("0")
        total_entry = ctx.total_entry_cost or Decimal("0")
        loser_proceeds = ctx.loser_exit_proceeds or Decimal("0")

        executable_price = self._executable_price(ctx.exit_eval)
        expected_survivor = Decimal("0")
        if executable_price is not None and qty > 0:
            gross = executable_price * qty
            fees = ctx.estimated_fees
            if fees <= 0 and cfg.estimated_fee_bps > 0:
                fees = gross * cfg.estimated_fee_bps / Decimal("10000")
            slippage = ctx.slippage_buffer * qty
            expected_survivor = gross - fees - slippage

        expected_net_total = loser_proceeds + expected_survivor - total_entry
        expected_net_per_pair = None
        if qty > 0:
            expected_net_per_pair = expected_net_total / qty

        verdict = EconomicsVerdict.PROCEED
        if ctx.phase == "pre_entry" and cfg.reject_entry_if_expected_net_below is not None:
            threshold = cfg.reject_entry_if_expected_net_below * qty if qty > 0 else cfg.reject_entry_if_expected_net_below
            if expected_net_total < threshold:
                verdict = EconomicsVerdict.ABORT_ENTRY
        elif ctx.phase == "survivor_hold" and cfg.exit_survivor_if_expected_net_below is not None:
            per_pair = expected_net_per_pair or Decimal("0")
            if per_pair < cfg.exit_survivor_if_expected_net_below:
                verdict = EconomicsVerdict.EXIT_SURVIVOR_EARLY
        elif expected_net_per_pair is not None and expected_net_per_pair < ctx.minimum_acceptable_net:
            verdict = EconomicsVerdict.DEFER

        if verdict in {EconomicsVerdict.ABORT_ENTRY, EconomicsVerdict.EXIT_SURVIVOR_EARLY}:
            if not is_enforce(cfg.enforcement_mode):
                verdict = EconomicsVerdict.PROCEED

        evidence = {
            "phase": ctx.phase,
            "expected_net_total": str(expected_net_total),
            "expected_net_per_pair": str(expected_net_per_pair) if expected_net_per_pair is not None else "",
            "executable_price": str(executable_price) if executable_price is not None else "",
            "expected_survivor_proceeds": str(expected_survivor),
            "total_entry_cost": str(total_entry),
            "loser_exit_proceeds": str(loser_proceeds),
            "estimated_fees": str(ctx.estimated_fees),
            "slippage_buffer": str(ctx.slippage_buffer),
            "enforcement_mode": cfg.enforcement_mode,
            "target_mode": ctx.selected_target_mode.value if ctx.selected_target_mode else "",
        }
        return EconomicsEvaluation(
            verdict=verdict,
            expected_net_total=expected_net_total,
            expected_net_per_pair=expected_net_per_pair,
            evidence=evidence,
        )

    @staticmethod
    def _executable_price(exit_eval: SurvivalExitEvaluation | None) -> Decimal | None:
        if exit_eval is None:
            return None
        ev = exit_eval.evidence
        return ev.sweep_vwap or ev.executable_bid or ev.touch_bid
