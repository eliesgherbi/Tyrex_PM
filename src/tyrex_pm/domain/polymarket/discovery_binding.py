"""Validated market binding for active or prepared-next sessions (N2).

Public identity for BTC 5m is **Up/Down** via ``outcomes`` / token helpers.
``BinaryMarket.yes`` / ``.no`` remain a generic-binary compatibility layer
where YES←Up and NO←Down for BTC; callers must not treat those slots as
literal venue YES/NO outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import TokenId
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.domain.polymarket.outcome_map import NormalizedLeg, OutcomeMap


class DiscoverySessionRole(str, Enum):
    ACTIVE = "active"
    PREPARED_NEXT = "prepared_next"
    CANDIDATE = "candidate"


@dataclass(frozen=True, kw_only=True)
class BookLegIdentity:
    """Public book subscription identity for one normalized leg."""

    leg: NormalizedLeg
    venue_label: str
    token_id: TokenId


@dataclass(frozen=True, kw_only=True)
class DiscoveredMarketBinding:
    """Discovery result with label-mapped outcomes and provenance metadata.

    The market runtime may hold a ``PREPARED_NEXT`` binding before promotion.
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
                require_utc(self.requested_window_start, field_name="requested_window_start"),
            )

    @property
    def outcome_semantics(self) -> str:
        legs = {b.leg for b in self.outcomes.legs}
        if NormalizedLeg.UP in legs and NormalizedLeg.DOWN in legs:
            return "UP_DOWN"
        if NormalizedLeg.YES in legs and NormalizedLeg.NO in legs:
            return "YES_NO"
        return "UNKNOWN"

    @property
    def up_token_id(self) -> str | None:
        try:
            return self.outcomes.token_for(NormalizedLeg.UP).value
        except KeyError:
            return None

    @property
    def down_token_id(self) -> str | None:
        try:
            return self.outcomes.token_for(NormalizedLeg.DOWN).value
        except KeyError:
            return None

    def require_up_down_tokens(self) -> tuple[TokenId, TokenId]:
        """Fail closed unless both Up and Down legs are label-mapped."""
        if self.outcome_semantics != "UP_DOWN":
            raise ValueError("binding is not Up/Down label-mapped")
        return self.outcomes.as_up_down()

    @property
    def book_legs(self) -> tuple[BookLegIdentity, ...]:
        return tuple(
            BookLegIdentity(leg=b.leg, venue_label=b.venue_label, token_id=b.token_id)
            for b in self.outcomes.legs
        )

    @property
    def clob_asset_ids(self) -> list[str]:
        """Token IDs for CLOB subscribe — order follows OutcomeMap legs, not position assumption."""
        return list(self.outcomes.token_ids)

    @property
    def compatibility_yes_no_note(self) -> str:
        return (
            "BinaryMarket.yes/no are generic-binary slots. For BTC Up/Down markets, "
            "yes holds the Up token and no holds the Down token. Public semantics are "
            "Up/Down via DiscoveredMarketBinding.outcomes / book_legs only."
        )
