"""Pure per-share taker fee φ(p) for economic edge math.

Reusable outside execution packages. Same curve as venue ``fd``:
``φ(p) = r · (p · (1 − p))^e`` for probability price ``p ∈ [0, 1]``.

F2 labels results as **estimated** planned fees — never confirmed actuals.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from tyrex_pm.core.numerics import as_decimal, require_polymarket_price


class FeeEstimateKind(str, Enum):
    """Distinguish planned/estimated fees from confirmed fill fees."""

    MAXIMUM_RESERVATION = "MAXIMUM_RESERVATION"
    ESTIMATED = "ESTIMATED"
    CONFIRMED_ACTUAL = "CONFIRMED_ACTUAL"


@dataclass(frozen=True, kw_only=True)
class FeeCurveParams:
    """Minimal fee-curve parameters (mirrors fd.r / fd.e)."""

    fee_rate: Decimal
    exponent: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fee_rate", as_decimal(self.fee_rate, field_name="fee_rate")
        )
        object.__setattr__(
            self, "exponent", as_decimal(self.exponent, field_name="exponent")
        )
        if self.fee_rate < 0 or self.exponent < 0:
            raise ValueError("fee_rate and exponent must be >= 0")


# Provisional sample until live market fd is supplied (labeled provisional).
PROVISIONAL_SAMPLE_FEE = FeeCurveParams(
    fee_rate=Decimal("0.07"),
    exponent=Decimal("1"),
)


@dataclass(frozen=True, kw_only=True)
class EstimatedUnitFee:
    """Per-share fee with explicit estimate kind (never silently 'confirmed')."""

    amount: Decimal
    kind: FeeEstimateKind
    price: Decimal
    curve: FeeCurveParams

    def __post_init__(self) -> None:
        if self.kind is FeeEstimateKind.CONFIRMED_ACTUAL:
            raise ValueError(
                "EstimatedUnitFee cannot carry CONFIRMED_ACTUAL; "
                "confirmed fees belong to fill/ledger paths"
            )
        object.__setattr__(self, "amount", as_decimal(self.amount, field_name="amount"))
        object.__setattr__(
            self, "price", require_polymarket_price(as_decimal(self.price, field_name="price"))
        )


def phi_taker_fee_per_share(price: Decimal, curve: FeeCurveParams) -> Decimal:
    """Dynamic taker fee per share at probability price ``price``."""
    p = require_polymarket_price(as_decimal(price, field_name="price"))
    if p == 0 or p == 1:
        return Decimal("0")
    base = p * (Decimal("1") - p)
    exp = curve.exponent
    if exp == exp.to_integral_value():
        return curve.fee_rate * (base ** int(exp))
    # Explicit float bridge only for non-integer exponents.
    return curve.fee_rate * Decimal(str(float(base) ** float(exp)))


def estimated_taker_fee_per_share(
    price: Decimal,
    curve: FeeCurveParams,
    *,
    kind: FeeEstimateKind = FeeEstimateKind.ESTIMATED,
) -> EstimatedUnitFee:
    if kind is FeeEstimateKind.CONFIRMED_ACTUAL:
        raise ValueError("use fill ledger for confirmed actual fees")
    amt = phi_taker_fee_per_share(price, curve)
    return EstimatedUnitFee(amount=amt, kind=kind, price=price, curve=curve)
