"""N7 sealed config + ceremony removal + fee-inclusive sizing."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.domain.polymarket.fees import FeeCurveParams
from tyrex_pm.runtime.live_config import LiveScope
from tyrex_pm.runtime.n7_authorization import (
    INVALIDATED_ENVELOPE_IDS,
    create_authorization_request,
)
from tyrex_pm.runtime.n7_sealed import (
    default_n7_sealed_config,
    load_n7_sealed_config,
    n7_sealed_from_mapping,
)
from tyrex_pm.runtime.n7_sizing import size_fee_inclusive_entry
from tyrex_pm.runtime.n7_timing import PRODUCTION_TIMING_VALUES_STATUS
from tyrex_pm.runtime.scope_a_ladder import PRODUCTION_TIMING_VALUES_STATUS as LADDER_STATUS

REPO = Path(__file__).resolve().parents[1]


def test_defaults_off_and_timing_frozen():
    cfg = default_n7_sealed_config()
    assert cfg.live.enabled is False
    assert cfg.live.mutations_enabled is False
    assert cfg.live.scope is LiveScope.A
    assert cfg.max_buy_collateral == Decimal("5.00")
    assert PRODUCTION_TIMING_VALUES_STATUS == "FROZEN_FOR_N7"
    assert LADDER_STATUS == "FROZEN_FOR_N7"


def test_sealed_config_file_loads():
    cfg = load_n7_sealed_config(REPO / "config" / "n7_tiny_live.json")
    assert cfg.live.mutations_enabled is False
    assert cfg.max_buy_collateral <= Decimal("5.00")
    assert cfg.resolution_capability is False


def test_cap_cannot_exceed_five():
    with pytest.raises(ValueError, match="5.00"):
        n7_sealed_from_mapping(
            {
                "max_buy_collateral": "5.01",
                "live": {"enabled": False, "mutations_enabled": False, "scope": "A"},
            }
        )


def test_authorization_ceremony_removed():
    assert "b3a95919-73a7-43e6-a1d8-3876dd09c2b6" in INVALIDATED_ENVELOPE_IDS
    assert "69cff32a-5f84-4b07-902e-dc56b7b93c80" in INVALIDATED_ENVELOPE_IDS
    with pytest.raises(RuntimeError, match="ceremony removed"):
        create_authorization_request()


def test_fee_inclusive_entry_never_exceeds_five():
    curve = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
    sized = size_fee_inclusive_entry(
        worst_price=Decimal("0.50"),
        collateral_cap=Decimal("5.00"),
        fee_curve=curve,
    )
    assert not isinstance(sized, type(None))
    from tyrex_pm.runtime.n7_sizing import FeeInclusiveEntrySize, FeeInclusiveSizeSkip

    assert isinstance(sized, FeeInclusiveEntrySize)
    assert sized.max_fee_inclusive_debit <= Decimal("5.00")
    assert sized.share_notional + sized.conservative_entry_fee <= Decimal("5.00")
    # quantity rounded down; recompute
    assert sized.quantity * sized.worst_price <= sized.share_notional + Decimal("0.000001")


def test_worst_price_used_and_downward_rounding():
    curve = FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
    a = size_fee_inclusive_entry(worst_price=Decimal("0.40"), fee_curve=curve)
    b = size_fee_inclusive_entry(worst_price=Decimal("0.60"), fee_curve=curve)
    from tyrex_pm.runtime.n7_sizing import FeeInclusiveEntrySize

    assert isinstance(a, FeeInclusiveEntrySize) and isinstance(b, FeeInclusiveEntrySize)
    # Higher price → fewer shares for same USDC budget
    assert b.quantity <= a.quantity


def test_venue_minimum_above_fee_inclusive_cap_skips():
    from tyrex_pm.runtime.n7_sizing import FeeInclusiveSizeSkip

    sized = size_fee_inclusive_entry(
        worst_price=Decimal("0.50"),
        collateral_cap=Decimal("5.00"),
        min_valid_order_notional=Decimal("5.00"),
        fee_curve=FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1")),
    )
    # With fees, max share notional < 5, so min=5 cannot fit
    assert isinstance(sized, FeeInclusiveSizeSkip)
    assert sized.reason == "venue_minimum_above_fee_inclusive_cap"
