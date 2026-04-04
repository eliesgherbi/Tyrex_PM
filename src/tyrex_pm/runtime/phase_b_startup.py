"""
Phase B (B5): informational startup summary for operators.

**No risk behavior** — format-only helper + log line from :func:`build_guru_trading_node`.
See ``Docs/OPERATIONS.md`` § Phase B and ``Phase_B_planing.md`` §10 B5.
"""

from __future__ import annotations

import math

from tyrex_pm.config.loaders import RiskSettings, RuntimeSettings, framework_phase_b_eligible


def phase_b_startup_summary_line(
    risk: RiskSettings,
    runtime: RuntimeSettings,
    *,
    b1_aggregator_wired: bool,
) -> str:
    """
    Single-line summary of Phase B gate **configuration** and path eligibility.

    ``b1_aggregator_wired`` is true when ``NautilusPortfolioExposureAggregator`` is injected
    (live + Nautilus live + framework submit). Framework-only gates (B2/B3) are only valid
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
        f"framework_truth_eligible={fw} b1_aggregator_wired={b1_aggregator_wired} "
        f"portfolio_notional_cap_usd={cap_s} max_concurrent_guru_resting_orders={conc_s} "
        f"fail_on_unresolved_portfolio_exposure={risk.fail_on_unresolved_portfolio_exposure} "
        f"collateral_reserve_usd={risk.collateral_reserve_usd} "
        f"capital_gate_enabled={risk.capital_gate_enabled}"
    )
