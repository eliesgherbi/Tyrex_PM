"""
Informational startup summary for operators (single INFO line).

**No risk behavior** — format-only helper + log line from :func:`build_guru_trading_node`.
See ``Docs/OPERATIONS.md`` (compose log line and gate semantics).
"""

from __future__ import annotations

import math

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings, framework_phase_b_eligible


def phase_b_startup_summary_line(
    risk: RiskSettings,
    runtime: RuntimeSettings,
    *,
    deployment_budget_wired: bool,
) -> str:
    """
    Single-line summary of framework-truth and deployment-budget **configuration** and path eligibility.

    ``deployment_budget_wired`` is true when :class:`~tyrex_pm.runtime.deployment_budget.NautilusDeploymentBudget`
    is injected (``execution_mode: live``). Framework-only gates are only valid
    when :func:`~tyrex_pm.config.loaders.framework_phase_b_eligible` is true — this string
    is emitted **after** :func:`~tyrex_pm.config.loaders.validate_phase_b_runtime_contract`.
    """
    fw = framework_phase_b_eligible(runtime)
    cap = risk.max_portfolio_notional_usd_open
    cap_s = "off" if math.isinf(cap) else str(cap)
    conc = risk.max_concurrent_guru_resting_orders
    conc_s = "off" if conc is None else str(conc)
    return (
        "tyrex_pm phase_b: "
        f"framework_truth_eligible={fw} deployment_budget_wired={deployment_budget_wired} "
        f"portfolio_deployment_cap_usd={cap_s} max_concurrent_guru_resting_orders={conc_s} "
        f"fail_on_unresolved_portfolio_deployment={risk.fail_on_unresolved_portfolio_deployment} "
        f"fail_on_unresolved_token_deployment={risk.fail_on_unresolved_token_deployment} "
        f"collateral_reserve_usd={risk.collateral_reserve_usd} "
        f"capital_gate_enabled={risk.capital_gate_enabled}"
    )
