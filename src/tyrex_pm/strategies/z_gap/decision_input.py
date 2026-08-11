"""Atomic immutable Z-Gap decision input."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId, MarketId
from tyrex_pm.core.time_authority import TimeAuthorityView
from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.strategies.z_gap.snapshots import DecisionEpoch, ZGapModelSnapshot
from tyrex_pm.strategies.z_gap.valuations import LegBookView, PositionView


@dataclass(frozen=True, kw_only=True)
class ZGapDecisionSnapshot:
    """One sealed decision epoch binding all Z-Gap evaluation inputs."""

    epoch: DecisionEpoch
    model: ZGapModelSnapshot
    time: TimeAuthorityView
    ptb: PtbSnapshot | None
    up_book: LegBookView
    down_book: LegBookView
    fee_curve: FeeCurveParams
    fee_resolved: bool
    target_notional: Decimal
    trigger: str  # "feed" | "timer"
    observed_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None = None
    position: PositionView | None = None  # hypothetical only when explicitly supplied
    capabilities: Mapping[str, bool] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trigger not in {"feed", "timer"}:
            raise ValueError("trigger must be 'feed' or 'timer'")
        object.__setattr__(
            self, "observed_at", require_utc(self.observed_at, field_name="observed_at")
        )
        if self.model.epoch.epoch_id != self.epoch.epoch_id:
            raise ValueError("model epoch must match decision epoch")
        if self.position is not None and self.position.epoch.epoch_id != self.epoch.epoch_id:
            raise ValueError("position epoch must match decision epoch")
        object.__setattr__(self, "capabilities", dict(self.capabilities))
        object.__setattr__(self, "evidence", dict(self.evidence))

    @property
    def market_id(self) -> MarketId:
        return self.epoch.market_id

    @property
    def window_id(self) -> str:
        return self.epoch.window_id
