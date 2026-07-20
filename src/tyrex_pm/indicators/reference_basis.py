"""Reference-alignment / basis indicator (reusable — threshold is policy-owned).

basis_bps = (S − S_settlement_ref) / S_settlement_ref × 10000
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.numerics import as_decimal


class BasisValidity(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    MISSING = "MISSING"
    INVALID = "INVALID"


@dataclass(frozen=True, kw_only=True)
class BasisResult:
    basis_bps: Decimal | None
    validity: BasisValidity
    ready: bool
    reason_code: str | None
    trading_ref: Decimal | None
    settlement_ref: Decimal | None
    trading_ref_fresh: bool
    settlement_ref_fresh: bool


def compute_basis_bps(
    *,
    trading_ref: Decimal | str | int | None,
    settlement_ref: Decimal | str | int | None,
    trading_ref_fresh: bool = True,
    settlement_ref_fresh: bool = True,
) -> BasisResult:
    """Pure basis calculation. Gate thresholds belong to Z-Gap config/policy."""
    if trading_ref is None or settlement_ref is None:
        return BasisResult(
            basis_bps=None,
            validity=BasisValidity.MISSING,
            ready=False,
            reason_code="missing_reference",
            trading_ref=None if trading_ref is None else as_decimal(trading_ref, field_name="trading_ref"),
            settlement_ref=(
                None
                if settlement_ref is None
                else as_decimal(settlement_ref, field_name="settlement_ref")
            ),
            trading_ref_fresh=trading_ref_fresh,
            settlement_ref_fresh=settlement_ref_fresh,
        )

    s = as_decimal(trading_ref, field_name="trading_ref")
    s_cl = as_decimal(settlement_ref, field_name="settlement_ref")
    if s <= 0 or s_cl <= 0:
        return BasisResult(
            basis_bps=None,
            validity=BasisValidity.INVALID,
            ready=False,
            reason_code="invalid_reference_price",
            trading_ref=s,
            settlement_ref=s_cl,
            trading_ref_fresh=trading_ref_fresh,
            settlement_ref_fresh=settlement_ref_fresh,
        )

    if not trading_ref_fresh or not settlement_ref_fresh:
        return BasisResult(
            basis_bps=None,
            validity=BasisValidity.STALE,
            ready=False,
            reason_code="stale_reference",
            trading_ref=s,
            settlement_ref=s_cl,
            trading_ref_fresh=trading_ref_fresh,
            settlement_ref_fresh=settlement_ref_fresh,
        )

    basis = ((s - s_cl) / s_cl) * Decimal("10000")
    return BasisResult(
        basis_bps=basis,
        validity=BasisValidity.VALID,
        ready=True,
        reason_code=None,
        trading_ref=s,
        settlement_ref=s_cl,
        trading_ref_fresh=trading_ref_fresh,
        settlement_ref_fresh=settlement_ref_fresh,
    )
