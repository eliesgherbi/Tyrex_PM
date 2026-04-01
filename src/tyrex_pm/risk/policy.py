"""Risk gate: v1 shadow uses an explicit all-pass stub; v1.06+ supplies fail-closed policies."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.core.types import OrderIntent


class RiskPolicy(Protocol):
    def evaluate(self, intent: OrderIntent) -> tuple[bool, str]:
        """Return (approved, reason_code)."""


class ShadowAllPassRisk:
    """
    Does **not** enforce portfolio or notional limits — only for `execution_mode=shadow`.

    Replace with a real `RiskPolicy` before any live execution path.
    """

    def evaluate(self, intent: OrderIntent) -> tuple[bool, str]:
        _ = intent
        return True, "shadow_all_pass"
