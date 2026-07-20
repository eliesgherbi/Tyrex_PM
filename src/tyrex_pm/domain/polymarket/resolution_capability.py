"""Reusable binary-market resolution capability contract (provider-independent)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ResolutionCapabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    DISABLED = "DISABLED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, kw_only=True)
class ResolutionCapability:
    """Explicit composition-supplied capability to hold and settle a binary position.

    Not inferred by strategies. Hosts/bindings pass this through DecisionContext.
    """

    status: ResolutionCapabilityStatus = ResolutionCapabilityStatus.DISABLED
    # Wall seconds before event_end after which sell exits are blocked while pending.
    ponr_before_event_end_s: float = 5.0
    reason_code: str | None = None

    @property
    def available(self) -> bool:
        return self.status is ResolutionCapabilityStatus.AVAILABLE

    def __post_init__(self) -> None:
        if self.ponr_before_event_end_s < 0:
            raise ValueError("ponr_before_event_end_s must be >= 0")


DISABLED_RESOLUTION = ResolutionCapability(
    status=ResolutionCapabilityStatus.DISABLED,
    reason_code="resolution_capability_disabled",
)
