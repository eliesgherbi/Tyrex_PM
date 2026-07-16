"""Fail-closed risk engine with deterministic policy ordering."""

from __future__ import annotations

from tyrex_pm.core.intents import EnterIntent
from tyrex_pm.risk.context import RiskContext
from tyrex_pm.risk.decision import PolicyResult, RiskDecision, new_decision_id
from tyrex_pm.risk.policies import DEFAULT_POLICY_ORDER, RiskPolicy
from tyrex_pm.risk.reasons import RiskReason


class RiskEngine:
    def __init__(self, policies: tuple[RiskPolicy, ...] | None = None) -> None:
        self._policies = policies or DEFAULT_POLICY_ORDER

    def evaluate(self, intent: EnterIntent, context: RiskContext) -> RiskDecision:
        results: list[PolicyResult] = []
        try:
            for policy in self._policies:
                result = policy.evaluate(intent, context)
                results.append(result)
        except Exception as exc:  # noqa: BLE001 — fail closed
            results.append(
                PolicyResult(
                    policy_id="internal",
                    approved=False,
                    reason_code=RiskReason.INTERNAL_POLICY_ERROR,
                    evidence={"error_type": type(exc).__name__},
                )
            )
            return self._decision(intent, context, results, force_deny=True)

        denied = [r for r in results if not r.approved]
        approved = len(denied) == 0
        if approved:
            # Register semantic key only after full approval (denied may retry later).
            context.dedup.register(
                intent.semantic_key(),
                intent_id=intent.intent_id.value,
                now=context.now,
            )
        return self._decision(intent, context, results, force_deny=not approved)

    def _decision(
        self,
        intent: EnterIntent,
        context: RiskContext,
        results: list[PolicyResult],
        *,
        force_deny: bool,
    ) -> RiskDecision:
        denied = [r for r in results if not r.approved]
        approved = (not force_deny) and not denied
        if approved:
            reason_codes = (RiskReason.APPROVED,)
        else:
            reason_codes = tuple(r.reason_code for r in denied) or (
                RiskReason.INTERNAL_POLICY_ERROR,
            )
        return RiskDecision(
            decision_id=new_decision_id(),
            intent_id=intent.intent_id,
            approved=approved,
            mode=context.mode,
            reason_codes=reason_codes,
            policy_results=tuple(results),
            evaluated_at=context.now,
            correlation_id=intent.correlation_id,
            causation_id=intent.causation_id,
            evidence={
                "semantic_key": intent.semantic_key(),
                "policy_count": len(results),
                "exposure_available": context.exposure_available,
            },
            config_fingerprint=context.risk_config.config_fingerprint,
        )
