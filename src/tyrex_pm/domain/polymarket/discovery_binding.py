"""Validated market binding for active or prepared-next sessions (N2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tyrex_pm.core.clock import require_utc
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.outcome_map import OutcomeMap


class DiscoverySessionRole(str, Enum):
    ACTIVE = "active"
    PREPARED_NEXT = "prepared_next"
    CANDIDATE = "candidate"


@dataclass(frozen=True, kw_only=True)
class DiscoveredMarketBinding:
    """Discovery result with label-mapped outcomes and provenance metadata.

    N4 may hold a ``PREPARED_NEXT`` binding without publishing it as active.
    """

    market: BinaryMarket
    outcomes: OutcomeMap
    session_role: DiscoverySessionRole
    window_slug: str
    resolution_source: str | None
    resolution_rule_fingerprint: str
    market_rule_ok: bool
    requested_window_start: datetime | None = None

    def __post_init__(self) -> None:
        if self.requested_window_start is not None:
            object.__setattr__(
                self,
                "requested_window_start",
                require_utc(
                    self.requested_window_start, field_name="requested_window_start"
                ),
            )

    @property
    def up_token_id(self) -> str | None:
        from tyrex_pm.domain.polymarket.outcome_map import NormalizedLeg

        try:
            return self.outcomes.token_for(NormalizedLeg.UP).value
        except KeyError:
            return None

    @property
    def down_token_id(self) -> str | None:
        from tyrex_pm.domain.polymarket.outcome_map import NormalizedLeg

        try:
            return self.outcomes.token_for(NormalizedLeg.DOWN).value
        except KeyError:
            return None

    @property
    def clob_asset_ids(self) -> list[str]:
        return list(self.outcomes.token_ids)
