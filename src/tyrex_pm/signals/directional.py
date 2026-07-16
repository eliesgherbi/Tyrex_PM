"""Directional signal builder for ReferenceMomentumStrategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.ids import CorrelationId, EventId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.signals import Signal
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.market_data.executable import ExecutableQuote


class Direction(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    FLAT = "FLAT"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class DirectionalSignal:
    direction: Direction
    observed_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None
    momentum: Decimal | None
    threshold: Decimal
    strength: Decimal | None
    selected_outcome: OutcomeSide | None
    reason_code: str
    evidence: dict[str, Any]

    def as_signal(self) -> Signal:
        return Signal(
            signal_type="directional",
            observed_at=self.observed_at,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            evidence={
                "direction": self.direction.value,
                "momentum": None if self.momentum is None else str(self.momentum),
                "threshold": str(self.threshold),
                "strength": None if self.strength is None else str(self.strength),
                "selected_outcome": None
                if self.selected_outcome is None
                else self.selected_outcome.value,
                "reason_code": self.reason_code,
                **self.evidence,
            },
        )


def _strength(momentum: Decimal, threshold: Decimal) -> Decimal:
    """Normalized distance beyond threshold, capped at 1."""
    if threshold <= 0:
        return Decimal("0")
    excess = abs(momentum) - threshold
    if excess <= 0:
        return Decimal("0")
    return min(Decimal("1"), excess / threshold)


def build_directional_signal(
    *,
    snapshot: DecisionSnapshot,
    momentum: Decimal | None,
    momentum_ready: bool,
    momentum_reason: str,
    threshold: Decimal,
    max_spread: Decimal,
) -> DirectionalSignal:
    evidence: dict[str, Any] = {
        "yes_spread": None if snapshot.yes_quote.spread is None else str(snapshot.yes_quote.spread),
        "no_spread": None if snapshot.no_quote.spread is None else str(snapshot.no_quote.spread),
        "yes_fresh": snapshot.yes_freshness.reason_code.value,
        "no_fresh": snapshot.no_freshness.reason_code.value,
        "ref_fresh": snapshot.reference_freshness.reason_code.value,
        "momentum_reason": momentum_reason,
    }
    base = dict(
        observed_at=snapshot.observed_at,
        correlation_id=snapshot.correlation_id,
        causation_id=snapshot.causation_id,
        momentum=momentum,
        threshold=threshold,
        evidence=evidence,
    )
    if not (
        snapshot.yes_freshness.is_fresh
        and snapshot.no_freshness.is_fresh
        and snapshot.reference_freshness.is_fresh
        and momentum_ready
        and momentum is not None
    ):
        return DirectionalSignal(
            direction=Direction.UNAVAILABLE,
            strength=None,
            selected_outcome=None,
            reason_code="DATA_UNAVAILABLE",
            **base,
        )

    spread_reason = _spread_reason(snapshot.yes_quote, max_spread) or _spread_reason(
        snapshot.no_quote, max_spread
    )
    if spread_reason is not None:
        return DirectionalSignal(
            direction=Direction.UNAVAILABLE,
            strength=None,
            selected_outcome=None,
            reason_code=spread_reason,
            **base,
        )

    if momentum >= threshold:
        return DirectionalSignal(
            direction=Direction.UP,
            strength=_strength(momentum, threshold),
            selected_outcome=OutcomeSide.YES,
            reason_code="MOMENTUM_UP",
            **base,
        )
    if momentum <= -threshold:
        return DirectionalSignal(
            direction=Direction.DOWN,
            strength=_strength(momentum, threshold),
            selected_outcome=OutcomeSide.NO,
            reason_code="MOMENTUM_DOWN",
            **base,
        )
    return DirectionalSignal(
        direction=Direction.FLAT,
        strength=Decimal("0"),
        selected_outcome=None,
        reason_code="MOMENTUM_NEUTRAL",
        **base,
    )


def _spread_reason(quote: ExecutableQuote, max_spread: Decimal) -> str | None:
    if quote.best_bid is None or quote.best_ask is None or quote.spread is None:
        return "BOOK_ONE_SIDED_OR_EMPTY"
    if quote.spread > max_spread:
        return "SPREAD_TOO_WIDE"
    return None
