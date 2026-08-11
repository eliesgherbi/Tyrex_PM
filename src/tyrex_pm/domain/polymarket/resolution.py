"""Binary-market resolution-rule contract (provider-independent)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId


class ComparisonRule(str, Enum):
    """How settlement value is compared to strike K for the UP outcome."""

    AT_OR_ABOVE_K = "AT_OR_ABOVE_K"
    STRICTLY_ABOVE_K = "STRICTLY_ABOVE_K"


@dataclass(frozen=True, kw_only=True)
class BinaryResolutionRule:
    """Smallest contract needed to interpret a binary UP/DOWN window.

    Does not hardcode Chainlink or any concrete settlement feed.
    """

    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    comparison: ComparisonRule = ComparisonRule.AT_OR_ABOVE_K
    resolution_reference_id: str | None = None
    resolution_reference_provenance: str | None = None

    def __post_init__(self) -> None:
        if not self.window_id.strip():
            raise ValueError("window_id must be non-empty")
        object.__setattr__(
            self, "event_start", require_utc(self.event_start, field_name="event_start")
        )
        object.__setattr__(self, "event_end", require_utc(self.event_end, field_name="event_end"))
        if self.event_end <= self.event_start:
            raise ValueError("event_end must be after event_start")
