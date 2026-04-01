"""Guru follow entry / mirror exit signal policies."""

from __future__ import annotations

from dataclasses import dataclass

from tyrex_pm.core.reason_codes import ReasonCode
from tyrex_pm.core.types import GuruTradeSignal


@dataclass(frozen=True, slots=True)
class SignalDecision:
    accept: bool
    reason_code: str
    detail: str | None = None


class GuruFollowEntryPolicy:
    """BUY on allowlisted token → candidate entry (v1 shadow)."""

    def __init__(self, allowlisted_token_ids: frozenset[str]) -> None:
        self._allow = allowlisted_token_ids

    def evaluate(self, sig: GuruTradeSignal) -> SignalDecision:
        tid = sig.token_id
        if not tid:
            return SignalDecision(False, ReasonCode.MISSING_TOKEN_ID, "no token on signal")
        if tid not in self._allow:
            return SignalDecision(False, ReasonCode.NOT_ALLOWLISTED, tid)
        if sig.side != "BUY":
            return SignalDecision(
                False,
                ReasonCode.COPY_SKIP,
                f"entry path ignores side={sig.side}",
            )
        return SignalDecision(True, ReasonCode.GURU_ENTRY_CANDIDATE, None)


class GuruMirrorExitPolicy:
    """SELL on allowlisted token → mirror exit hypothesis (v1 shadow)."""

    def __init__(self, allowlisted_token_ids: frozenset[str]) -> None:
        self._allow = allowlisted_token_ids

    def evaluate(self, sig: GuruTradeSignal) -> SignalDecision:
        tid = sig.token_id
        if not tid:
            return SignalDecision(False, ReasonCode.MISSING_TOKEN_ID, "no token on signal")
        if tid not in self._allow:
            return SignalDecision(False, ReasonCode.NOT_ALLOWLISTED, tid)
        if sig.side != "SELL":
            return SignalDecision(
                False,
                ReasonCode.COPY_SKIP,
                f"exit path ignores side={sig.side}",
            )
        return SignalDecision(True, ReasonCode.GURU_EXIT_MIRROR, None)
