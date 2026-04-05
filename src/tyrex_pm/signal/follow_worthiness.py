"""C2 capital-efficiency gate: minimum follow notional (policy, not risk)."""

from __future__ import annotations

from tyrex_pm.core.reason_codes import ReasonCode


class FollowWorthinessGate:
    """
    If ``min_follow_notional_usd > 0``, require ``price_ref * qty >= min`` for worthiness.
    Missing ``price_ref`` when the threshold is enabled → dedicated policy skip (not risk).
    """

    def __init__(self, min_follow_notional_usd: float) -> None:
        if min_follow_notional_usd < 0:
            raise ValueError("min_follow_notional_usd must be >= 0")
        self._min = min_follow_notional_usd

    def evaluate(
        self,
        *,
        price_ref: float | None,
        qty: float,
    ) -> tuple[bool, str | None]:
        """Return ``(True, None)`` if OK, else ``(False, reason_code_str)``."""
        if self._min <= 0:
            return True, None
        if price_ref is None:
            return False, ReasonCode.MIN_FOLLOW_NOTIONAL_PRICE_MISSING.value
        n = float(price_ref) * float(qty)
        if n < self._min:
            return False, ReasonCode.MIN_FOLLOW_NOTIONAL.value
        return True, None
