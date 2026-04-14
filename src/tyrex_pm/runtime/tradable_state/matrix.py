"""
Frozen §10 matrix — :mod:`tyrex_pm.risk.configured` only; do not duplicate in strategy.

Source: ``Docs/Implementation/refactor_lifecycle/tradable_state_health.md`` §10.
"""

from __future__ import annotations

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.runtime.tradable_state.types import TradableStateHealth, TradableStateHealthSnapshot


def tradable_health_allows_intent(
    snap: TradableStateHealthSnapshot,
    *,
    side_upper: str,
    allow_exit_when_degraded_oms: bool,
) -> tuple[bool, str]:
    """
    Return ``(True, "approved")`` if health permits the side; else ``(False, reason_code)``.

    SELL under ``DEGRADED_OMS`` requires ``allow_exit_when_degraded_oms``; inventory gates still
    apply later in :class:`~tyrex_pm.risk.configured.ConfiguredRiskPolicy`.
    """
    lev = snap.level
    if lev == TradableStateHealth.HEALTHY:
        return True, "approved"
    if lev == TradableStateHealth.UNKNOWN_BOOTSTRAP:
        return False, str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)
    if lev == TradableStateHealth.DIVERGENT_PERSISTENT:
        return False, str(ReasonCode.RISK_HEALTH_DIVERGENT_PERSISTENT)
    if lev == TradableStateHealth.DEGRADED_OMS:
        if side_upper == "BUY":
            return False, str(ReasonCode.RISK_HEALTH_DEGRADED_OMS)
        if side_upper == "SELL":
            if allow_exit_when_degraded_oms:
                return True, "approved"
            return False, str(ReasonCode.RISK_HEALTH_DEGRADED_OMS)
        return False, str(ReasonCode.RISK_HEALTH_DEGRADED_OMS)
    return False, str(ReasonCode.RISK_HEALTH_UNKNOWN_BOOTSTRAP)
