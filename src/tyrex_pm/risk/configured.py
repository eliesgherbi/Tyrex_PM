"""Fail-closed risk gate from :class:`~tyrex_pm.config.loaders.RiskSettings`."""

from __future__ import annotations

from collections import defaultdict

from tyrex_pm.config.loaders import RiskSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import OrderIntent


class ConfiguredRiskPolicy:
    """
    Session-local exposure accounting (approximates open notional per token).

    Uses guru `price_ref` × `quantity` when available; **does not** read venue positions
    (integrate cache/portfolio when Nautilus positions are wired).
    """

    def __init__(self, settings: RiskSettings) -> None:
        self._s = settings
        self._token_open: dict[str, float] = defaultdict(float)

    def note_fill_assumption(self, intent: OrderIntent) -> None:
        """Call after a **live** submit succeeds to track session exposure (best-effort)."""
        n = _estimate_notional(intent)
        if n is not None and n > 0:
            self._token_open[intent.token_id] += n

    def evaluate(self, intent: OrderIntent) -> tuple[bool, str]:
        if self._s.kill_switch:
            return False, ReasonCode.RISK_KILL_SWITCH

        if intent.quantity > self._s.max_order_quantity:
            return False, ReasonCode.RISK_ORDER_QTY_LIMIT

        n = _estimate_notional(intent)
        if n is None:
            if self._s.fail_on_missing_price_for_notional:
                return False, ReasonCode.RISK_MISSING_PRICE
        else:
            if n > self._s.max_notional_usd_per_order:
                return False, ReasonCode.RISK_NOTIONAL_PER_ORDER
            next_open = self._token_open[intent.token_id] + n
            if next_open > self._s.max_token_notional_usd_open:
                return False, ReasonCode.RISK_TOKEN_NOTIONAL_OPEN

        return True, "approved"


def _estimate_notional(intent: OrderIntent) -> float | None:
    if intent.price_ref is None:
        return None
    return float(intent.price_ref) * float(intent.quantity)
