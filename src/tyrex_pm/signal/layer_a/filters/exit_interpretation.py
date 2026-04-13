"""Exit interpretation: mirror guru vs full follower position."""

from __future__ import annotations

from tyrex_pm.config.loaders import ExitFilterSettings
from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.layer_a.types import Branch, LayerAContext, LayerAOutcome


class ExitInterpretationFilter:
    name = "exit_interpretation"

    def __init__(self, settings: ExitFilterSettings) -> None:
        self._settings = settings

    def evaluate(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        ctx: LayerAContext | None,
    ) -> LayerAOutcome:
        if branch != "exit":
            return LayerAOutcome(True, str(ReasonCode.LAYER_A_EXIT_MIRROR_OK), None, {})
        if not self._settings.enabled or self._settings.exit_method == "mirror_guru":
            return LayerAOutcome(
                True,
                str(ReasonCode.LAYER_A_EXIT_MIRROR_OK),
                None,
                {"exit_qty_mode": "mirror_guru"},
            )
        tid = sig.token_id
        if not tid:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_EXIT_FULL_DENIED_INVALID_TOKEN),
                "missing token_id",
            )
        if ctx is None:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_EXIT_FULL_DENIED_UNRESOLVED),
                "layer_a_context missing",
            )
        try:
            qty = ctx.follower_long_qty_for_outcome_token(str(tid))
        except Exception as exc:  # noqa: BLE001
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_EXIT_FULL_DENIED_UNREADABLE),
                f"{type(exc).__name__}: {exc}",
            )
        if qty is None:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_EXIT_FULL_DENIED_UNRESOLVED),
                "instrument or position unresolved",
            )
        if qty <= 0.0:
            return LayerAOutcome(
                False,
                str(ReasonCode.LAYER_A_EXIT_FULL_DENIED_NO_POSITION),
                f"follower_long_qty={qty}",
            )
        return LayerAOutcome(
            True,
            str(ReasonCode.LAYER_A_EXIT_INTERPRETATION_OK),
            None,
            {"exit_qty_mode": "full_position", "follower_position_qty": float(qty)},
        )
