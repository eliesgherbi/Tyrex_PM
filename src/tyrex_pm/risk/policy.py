"""Risk gate: v1 shadow uses an explicit all-pass stub; v1.06+ supplies fail-closed policies."""

from __future__ import annotations

from typing import Protocol

from tyrex_pm.core.types import OrderIntent


class RiskPolicy(Protocol):
    def evaluate(self, intent: OrderIntent) -> tuple[bool, str, OrderIntent | None]:
        """Return (approved, reason_code, intent_to_submit). ``intent_to_submit`` is set when approved."""


class ShadowAllPassRisk:
    """
    Does **not** enforce portfolio or notional limits — only for `execution_mode=shadow`.

    Replace with a real `RiskPolicy` before any live execution path.
    """

    def evaluate(self, intent: OrderIntent) -> tuple[bool, str, OrderIntent | None]:
        return True, "shadow_all_pass", intent
