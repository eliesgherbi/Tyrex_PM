"""Tests for Z-Gap fixed USD sizing (A0.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tyrex_pm.runtime.config import ZGapSizingConfig
from tyrex_pm.strategies.z_gap.sizing import (
    REASON_MISSING_LIMIT_PRICE,
    REASON_NOTIONAL_CAP,
    REASON_SIZE_BELOW_MIN,
    REASON_VENUE_MIN_SIZE,
    ZGapSizingError,
    compute_fixed_usd_shares,
)


def _sizing(*, max_usd: str = "5", min_shares: str = "5") -> ZGapSizingConfig:
    return ZGapSizingConfig(mode="fixed_usd", max_usd=Decimal(max_usd), min_shares=Decimal(min_shares))


def test_max_usd_over_limit_price_computes_shares() -> None:
    result = compute_fixed_usd_shares(limit_price=Decimal("0.50"), sizing=_sizing())
    assert result.shares == Decimal("10")
    assert result.notional_usd == Decimal("5.0")


def test_size_floors_down() -> None:
    result = compute_fixed_usd_shares(limit_price=Decimal("0.61"), sizing=_sizing(max_usd="5"))
    assert result.shares == Decimal("8")
    assert result.notional_usd <= Decimal("5")


def test_notional_never_exceeds_max_usd() -> None:
    result = compute_fixed_usd_shares(limit_price=Decimal("0.40"), sizing=_sizing(max_usd="5"))
    assert result.notional_usd <= Decimal("5")


def test_blocks_below_min_shares() -> None:
    with pytest.raises(ZGapSizingError, match=REASON_SIZE_BELOW_MIN):
        compute_fixed_usd_shares(
            limit_price=Decimal("0.99"),
            sizing=ZGapSizingConfig(mode="fixed_usd", max_usd=Decimal("4"), min_shares=Decimal("5")),
        )


def test_blocks_missing_limit_price() -> None:
    with pytest.raises(ZGapSizingError, match=REASON_MISSING_LIMIT_PRICE):
        compute_fixed_usd_shares(limit_price=None, sizing=_sizing())


def test_respects_venue_min_order_size() -> None:
    with pytest.raises(ZGapSizingError, match=REASON_VENUE_MIN_SIZE):
        compute_fixed_usd_shares(
            limit_price=Decimal("0.90"),
            sizing=_sizing(max_usd="5", min_shares="1"),
            venue_min_order_size=Decimal("10"),
        )


def test_notional_cap_guard() -> None:
    # Construct pathological case where floor division still passes min but exceeds max
    sizing = ZGapSizingConfig(mode="fixed_usd", max_usd=Decimal("4.99"), min_shares=Decimal("1"))
    result = compute_fixed_usd_shares(limit_price=Decimal("0.50"), sizing=sizing)
    assert result.notional_usd <= Decimal("4.99")
