"""Fixed USD sizing for Z-Gap entry plans (A0.6)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from tyrex_pm.runtime.config import ZGapSizingConfig

REASON_SIZE_BELOW_MIN = "z_gap_entry_blocked_size_below_min"
REASON_NOTIONAL_CAP = "z_gap_entry_blocked_notional_cap"
REASON_MISSING_LIMIT_PRICE = "z_gap_entry_blocked_missing_limit_price"
REASON_VENUE_MIN_SIZE = "z_gap_entry_blocked_venue_min_size"


class ZGapSizingError(ValueError):
    """Fail-closed sizing rejection."""


@dataclass(frozen=True)
class ZGapSizingResult:
    shares: Decimal
    notional_usd: Decimal


def compute_fixed_usd_shares(
    *,
    limit_price: Decimal | None,
    sizing: ZGapSizingConfig,
    venue_min_order_size: Decimal | None = None,
) -> ZGapSizingResult:
    """Compute share count from fixed USD cap: ``floor(max_usd / limit_price)``."""
    if limit_price is None or limit_price <= 0:
        raise ZGapSizingError(REASON_MISSING_LIMIT_PRICE)

    shares = (sizing.max_usd / limit_price).to_integral_value(rounding=ROUND_DOWN)
    if shares < sizing.min_shares:
        raise ZGapSizingError(REASON_SIZE_BELOW_MIN)

    min_required = sizing.min_shares
    if venue_min_order_size is not None and venue_min_order_size > min_required:
        min_required = venue_min_order_size
    if shares < min_required:
        raise ZGapSizingError(REASON_VENUE_MIN_SIZE)

    notional = shares * limit_price
    if notional > sizing.max_usd:
        raise ZGapSizingError(REASON_NOTIONAL_CAP)

    return ZGapSizingResult(shares=shares, notional_usd=notional)
