"""Layer A orchestrator — evaluation order per ``00_general_plan.md``."""

from __future__ import annotations

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.layer_a.filters.exit_interpretation import ExitInterpretationFilter
from tyrex_pm.signal.layer_a.filters.significance_conviction import SignificanceConvictionFilter
from tyrex_pm.signal.layer_a.filters.static_amount import StaticAmountGatingFilter
from tyrex_pm.signal.layer_a.filters.token_allowlist import TokenAllowlistGatingFilter
from tyrex_pm.signal.layer_a.types import Branch, LayerAContext, LayerAOutcome, LayerAStepRecord


class LayerAOrchestrator:
    def __init__(
        self,
        *,
        token: TokenAllowlistGatingFilter,
        static: StaticAmountGatingFilter,
        significance: SignificanceConvictionFilter,
        exit_interpretation: ExitInterpretationFilter,
    ) -> None:
        self._token = token
        self._static = static
        self._significance = significance
        self._exit = exit_interpretation

    def run(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        ctx: LayerAContext | None,
    ) -> tuple[LayerAOutcome, list[LayerAStepRecord]]:
        records: list[LayerAStepRecord] = []

        def _rec(name: str, o: LayerAOutcome) -> None:
            records.append(
                LayerAStepRecord(
                    filter_name=name,
                    branch=branch,
                    accept=o.accept,
                    reason_code=o.reason_code,
                    detail=o.detail,
                    metadata=dict(o.metadata),
                ),
            )

        token_out = self._token.evaluate(sig, branch=branch, ctx=ctx)
        _rec(self._token.name, token_out)
        if not token_out.accept:
            self._post_entry_observe(sig, branch=branch, token_passed=False)
            return token_out, records

        if branch == "exit":
            exit_out = self._exit.evaluate(sig, branch=branch, ctx=ctx)
            _rec(self._exit.name, exit_out)
            if not exit_out.accept:
                return exit_out, records
            return (
                LayerAOutcome(
                    True,
                    str(ReasonCode.GURU_EXIT_MIRROR),
                    None,
                    dict(exit_out.metadata),
                ),
                records,
            )

        static_out = self._static.evaluate(sig, branch=branch, ctx=ctx)
        _rec(self._static.name, static_out)
        if not static_out.accept:
            self._post_entry_observe(sig, branch=branch, token_passed=True)
            return static_out, records

        sig_out = self._significance.evaluate(sig, branch=branch, ctx=ctx)
        _rec(self._significance.name, sig_out)
        if not sig_out.accept:
            self._post_entry_observe(sig, branch=branch, token_passed=True)
            return sig_out, records

        self._post_entry_observe(sig, branch=branch, token_passed=True)
        return (
            LayerAOutcome(
                True,
                str(ReasonCode.GURU_ENTRY_CANDIDATE),
                None,
                {},
            ),
            records,
        )

    def _post_entry_observe(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        token_passed: bool,
    ) -> None:
        if branch != "entry":
            return
        self._significance.observe_buy(sig, token_gating_passed=token_passed)
