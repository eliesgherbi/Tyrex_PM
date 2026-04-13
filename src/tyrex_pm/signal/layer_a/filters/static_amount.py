"""Static USD floor gating (entry / BUY only)."""

from __future__ import annotations

from tyrex_pm.config.loaders import StaticAmountSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.layer_a.notional import notional_usd
from tyrex_pm.signal.layer_a.types import Branch, LayerAContext, LayerAOutcome


class StaticAmountGatingFilter:
    name = "static_amount"

    def __init__(self, settings: StaticAmountSettings) -> None:
        self._settings = settings

    def evaluate(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        ctx: LayerAContext | None = None,
    ) -> LayerAOutcome:
        _ = ctx
        if branch != "entry" or not self._settings.enabled:
            return LayerAOutcome(True, str(ReasonCode.LAYER_A_STATIC_AMOUNT_OK), None, {})
        if sig.price_raw is None:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_STATIC_AMOUNT_PRICE_MISSING),
                "price_raw null",
            )
        if sig.size_raw is None:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_STATIC_AMOUNT_SIZE_MISSING),
                "size_raw null",
            )
        try:
            px = float(sig.price_raw)
            sz = float(sig.size_raw)
        except (TypeError, ValueError):
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_STATIC_AMOUNT_INVALID_PRICE),
                "non-numeric price or size",
            )
        if px <= 0.0:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_STATIC_AMOUNT_INVALID_PRICE),
                f"price_raw={px}",
            )
        if sz <= 0.0:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_STATIC_AMOUNT_INVALID_SIZE),
                f"size_raw={sz}",
            )
        nu = px * sz
        th = float(self._settings.amount_usd)
        if nu < th:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_DENY_STATIC_AMOUNT_BELOW_THRESHOLD),
                f"notional_usd={nu} threshold={th}",
                {"notional_usd": nu, "threshold_usd": th},
            )
        return LayerAOutcome(
            True,
            str(ReasonCode.LAYER_A_STATIC_AMOUNT_OK),
            None,
            {"notional_usd": nu, "threshold_usd": th},
        )
