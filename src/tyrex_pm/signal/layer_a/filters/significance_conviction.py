"""BUY-only median significance gating."""

from __future__ import annotations

import statistics
from collections import deque

from tyrex_pm.config.loaders import SignificanceConvictionSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.layer_a.notional import notional_usd
from tyrex_pm.signal.layer_a.types import Branch, LayerAContext, LayerAOutcome


class SignificanceConvictionFilter:
    name = "significance_conviction"

    def __init__(self, settings: SignificanceConvictionSettings) -> None:
        self._settings = settings
        self._buf: deque[float] = deque(maxlen=max(1, int(settings.lookback_trades)))

    def evaluate(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        ctx: LayerAContext | None = None,
    ) -> LayerAOutcome:
        _ = ctx
        if branch != "entry" or not self._settings.enabled:
            return LayerAOutcome(True, str(ReasonCode.LAYER_A_SIGNIFICANCE_OK), None, {})
        current = notional_usd(sig)
        if current is None:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_SIGNIFICANCE_NOTIONAL_MISSING),
                "notional not computable",
            )
        prior = list(self._buf)
        if len(prior) == 0:
            return LayerAOutcome(
                True,
                str(ReasonCode.LAYER_A_SIGNIFICANCE_OK),
                None,
                {
                    "significance_cold_start": True,
                    "current_notional": current,
                    "window_len_prior": 0,
                },
            )
        med = float(statistics.median(prior))
        if current > med:
            return LayerAOutcome(
                True,
                str(ReasonCode.LAYER_A_SIGNIFICANCE_OK),
                None,
                {
                    "current_notional": current,
                    "median_prior": med,
                    "window_len_prior": len(prior),
                },
            )
        return LayerAOutcome(
            False,
            str(ReasonCode.LAYER_A_DENY_SIGNIFICANCE_MEDIAN),
            f"current={current} median_prior={med}",
            {
                "current_notional": current,
                "median_prior": med,
                "window_len_prior": len(prior),
            },
        )

    def observe_buy(self, sig: GuruTradeSignal, *, token_gating_passed: bool) -> None:
        """Append BUY notional after full entry chain when enabled and computable."""
        if not self._settings.enabled or not token_gating_passed:
            return
        if sig.side != "BUY":
            return
        current = notional_usd(sig)
        if current is None:
            return
        self._buf.append(float(current))
