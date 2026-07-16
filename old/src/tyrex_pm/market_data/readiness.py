"""Market readiness state machine (Phase 2 M3 / M8).

Tracks progression toward ``TRADING_ENABLED`` (WS_PRIMARY + quality PASS on both
legs). ``REST_BOOTSTRAP`` and ``REST_RECOVERY`` alone never reach ``TRADING_ENABLED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import TokenId
from tyrex_pm.market_data.models import SourceQuality
from tyrex_pm.market_data.quality import DataQualityGate, DecisionContext, QualityVerdict
from tyrex_pm.state.market_store import MarketStateStore


class MarketReadinessState(str, Enum):
    STARTING = "starting"
    REST_BOOTSTRAPPED = "rest_bootstrapped"
    WS_CONNECTED = "ws_connected"
    WS_BOOK_RECEIVED = "ws_book_received"
    BOTH_LEGS_READY = "both_legs_ready"
    QUALITY_PASS = "quality_pass"
    TRADING_ENABLED = "trading_enabled"
    PAUSED = "paused"
    DEGRADED = "degraded"


@dataclass
class MarketReadinessTracker:
    """In-memory readiness tracker for paired-binary market data."""

    yes_token_id: TokenId | None = None
    no_token_id: TokenId | None = None
    state: MarketReadinessState = MarketReadinessState.STARTING
    ws_connected: bool = False
    reconnect_gap: bool = False
    yes_ws_ready: bool = False
    no_ws_ready: bool = False
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def note_rest_bootstrapped(self) -> None:
        self._transition(MarketReadinessState.REST_BOOTSTRAPPED, reason="rest_bootstrap")

    def note_rest_recovery(self) -> None:
        """REST resync after gap — never sufficient alone for TRADING_ENABLED."""
        self.yes_ws_ready = False
        self.no_ws_ready = False
        if self.state == MarketReadinessState.TRADING_ENABLED:
            self._transition(MarketReadinessState.PAUSED, reason="rest_recovery")
        elif self.state not in (
            MarketReadinessState.PAUSED,
            MarketReadinessState.DEGRADED,
        ):
            self._transition(MarketReadinessState.DEGRADED, reason="rest_recovery")

    def note_ws_connected(self) -> None:
        self.ws_connected = True
        if self.state in (MarketReadinessState.STARTING, MarketReadinessState.REST_BOOTSTRAPPED):
            self._transition(MarketReadinessState.WS_CONNECTED, reason="ws_connected")

    def note_ws_disconnected(self) -> None:
        self.ws_connected = False
        self.yes_ws_ready = False
        self.no_ws_ready = False
        self._transition(MarketReadinessState.PAUSED, reason="ws_disconnected")

    def note_reconnect_gap(self) -> None:
        self.reconnect_gap = True
        self.yes_ws_ready = False
        self.no_ws_ready = False
        self._transition(MarketReadinessState.PAUSED, reason="reconnect_gap")

    def note_ws_book(self, token_id: TokenId, *, source_quality: str) -> None:
        if source_quality != SourceQuality.WS_PRIMARY:
            return
        if token_id == self.yes_token_id:
            self.yes_ws_ready = True
        elif token_id == self.no_token_id:
            self.no_ws_ready = True
        if self.state in (
            MarketReadinessState.STARTING,
            MarketReadinessState.REST_BOOTSTRAPPED,
            MarketReadinessState.WS_CONNECTED,
        ) and (self.yes_ws_ready or self.no_ws_ready):
            self._transition(MarketReadinessState.WS_BOOK_RECEIVED, reason="ws_book_received")

    def refresh(
        self,
        store: MarketStateStore,
        *,
        gate: DataQualityGate,
        pair_id: str,
        size,
        now=None,
    ) -> MarketReadinessState:
        if self.yes_token_id is None or self.no_token_id is None:
            return self.state
        if self.reconnect_gap:
            yes_gap = store.reconnect_gap(self.yes_token_id)
            no_gap = store.reconnect_gap(self.no_token_id)
            if not yes_gap and not no_gap:
                self.reconnect_gap = False

        pair = store.capture_pair(self.yes_token_id, self.no_token_id, pair_id, now=now)
        if pair is None:
            return self.state

        yes_sq = pair.yes.source_quality
        no_sq = pair.no.source_quality
        if yes_sq == SourceQuality.WS_PRIMARY:
            self.yes_ws_ready = True
        if no_sq == SourceQuality.WS_PRIMARY:
            self.no_ws_ready = True

        if self.yes_ws_ready and self.no_ws_ready:
            if self.state in (
                MarketReadinessState.STARTING,
                MarketReadinessState.REST_BOOTSTRAPPED,
                MarketReadinessState.WS_CONNECTED,
                MarketReadinessState.WS_BOOK_RECEIVED,
                MarketReadinessState.PAUSED,
                MarketReadinessState.DEGRADED,
            ):
                self._transition(MarketReadinessState.BOTH_LEGS_READY, reason="both_legs_ws_primary")

        report = gate.evaluate_pair(pair, context=DecisionContext.ENTRY, size=size)
        if report.reconnect_gap:
            self.reconnect_gap = True
            if self.state == MarketReadinessState.TRADING_ENABLED:
                self._transition(MarketReadinessState.PAUSED, reason="reconnect_gap")

        if report.verdict == QualityVerdict.PASS and self.state == MarketReadinessState.BOTH_LEGS_READY:
            self._transition(MarketReadinessState.QUALITY_PASS, reason="quality_pass")
        if (
            report.verdict == QualityVerdict.PASS
            and self.state == MarketReadinessState.QUALITY_PASS
            and self.ws_connected
            and not self.reconnect_gap
        ):
            self._transition(MarketReadinessState.TRADING_ENABLED, reason="trading_enabled")
        if report.verdict in (QualityVerdict.DEGRADED, QualityVerdict.EMERGENCY_ONLY):
            if self.state == MarketReadinessState.TRADING_ENABLED:
                self._transition(MarketReadinessState.DEGRADED, reason=report.verdict.value)
        if report.verdict == QualityVerdict.REJECT_DECISION and self.ws_connected:
            if self.state == MarketReadinessState.TRADING_ENABLED:
                self._transition(MarketReadinessState.PAUSED, reason="quality_reject")
        return self.state

    def allows_trading(self) -> bool:
        return (
            self.state == MarketReadinessState.TRADING_ENABLED
            and self.ws_connected
            and not self.reconnect_gap
        )

    def allows_new_entries(self) -> bool:
        return self.allows_trading()

    def _transition(self, new_state: MarketReadinessState, *, reason: str) -> None:
        if new_state == self.state:
            return
        self.transitions.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason}
        )
        self.state = new_state

    def drain_transitions(self) -> list[dict[str, Any]]:
        out = list(self.transitions)
        self.transitions.clear()
        return out
