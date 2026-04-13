"""Token allowlist + branch side check (parity with ``signal.entry`` policies)."""

from __future__ import annotations

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.signal.layer_a.types import Branch, LayerAContext, LayerAOutcome
from tyrex_pm.signal.token_filter_spec import TokenFilterSpec


class TokenAllowlistGatingFilter:
    name = "token_allowlist"

    def __init__(self, tokens: TokenFilterSpec) -> None:
        self._tokens = tokens

    def evaluate(
        self,
        sig: GuruTradeSignal,
        *,
        branch: Branch,
        ctx: LayerAContext | None = None,
    ) -> LayerAOutcome:
        _ = ctx
        tid = sig.token_id
        if not tid:
            return LayerAOutcome(
                False,
                str(ReasonCode.MISSING_TOKEN_ID),
                "no token on signal",
            )
        if not self._tokens.allows_token(tid):
            return LayerAOutcome(False, str(ReasonCode.NOT_ALLOWLISTED), tid)
        if branch == "entry" and sig.side != "BUY":
            return LayerAOutcome(
                False,
                str(ReasonCode.COPY_SKIP),
                f"entry path ignores side={sig.side}",
            )
        if branch == "exit" and sig.side != "SELL":
            return LayerAOutcome(
                False,
                str(ReasonCode.COPY_SKIP),
                f"exit path ignores side={sig.side}",
            )
        return LayerAOutcome(True, str(ReasonCode.LAYER_A_TOKEN_ALLOWLIST_OK), None, {})
