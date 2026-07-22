"""Fee-inclusive entry sizing for N7 ($5 hard maximum debit).

Rule: worst-case share notional + conservative entry fee ≤ cap.
Quantity is sized downward; venue precision respected; fail closed.
Exit fees never block inventory-reducing exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from tyrex_pm.domain.polymarket.fees import FeeCurveParams, phi_taker_fee_per_share
from tyrex_pm.execution.polymarket.fees_fd import (
    FeeDescriptor,
    FeeError,
    max_buy_amount_under_collateral_cap,
    taker_fee_usdc_for_shares,
)


@dataclass(frozen=True, kw_only=True)
class FeeInclusiveEntrySize:
    worst_price: Decimal
    share_notional: Decimal
    quantity: Decimal
    conservative_entry_fee: Decimal
    max_fee_inclusive_debit: Decimal
    collateral_cap: Decimal
    fee_curve: FeeCurveParams

    def to_dict(self) -> dict[str, str]:
        return {
            "worst_price": str(self.worst_price),
            "share_notional": str(self.share_notional),
            "quantity": str(self.quantity),
            "conservative_entry_fee": str(self.conservative_entry_fee),
            "max_fee_inclusive_debit": str(self.max_fee_inclusive_debit),
            "collateral_cap": str(self.collateral_cap),
        }


@dataclass(frozen=True, kw_only=True)
class FeeInclusiveSizeSkip:
    reason: str
    worst_price: Decimal | None
    collateral_cap: Decimal
    min_valid_order_notional: Decimal | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "reason": self.reason,
            "worst_price": None if self.worst_price is None else str(self.worst_price),
            "collateral_cap": str(self.collateral_cap),
            "min_valid_order_notional": (
                None
                if self.min_valid_order_notional is None
                else str(self.min_valid_order_notional)
            ),
        }


def _curve_to_descriptor(curve: FeeCurveParams) -> FeeDescriptor:
    return FeeDescriptor(
        fee_rate=curve.fee_rate,
        exponent=curve.exponent,
        taker_only=True,
        source="n7_sealed_provisional",
    )


def size_fee_inclusive_entry(
    *,
    worst_price: Decimal,
    collateral_cap: Decimal = Decimal("5.00"),
    fee_curve: FeeCurveParams | None = None,
    qty_step: Decimal = Decimal("0.000001"),
    money_step: Decimal = Decimal("0.01"),
    min_valid_order_notional: Decimal | None = None,
    desired_share_notional: Decimal | None = None,
) -> FeeInclusiveEntrySize | FeeInclusiveSizeSkip:
    """Size a BUY so share_notional + entry_fee ≤ collateral_cap at worst_price."""
    if collateral_cap > Decimal("5.00"):
        raise ValueError("collateral_cap must be <= 5.00")
    if worst_price <= 0 or worst_price >= 1:
        return FeeInclusiveSizeSkip(
            reason="invalid_worst_price",
            worst_price=worst_price,
            collateral_cap=collateral_cap,
        )
    curve = fee_curve or FeeCurveParams(fee_rate=Decimal("0.07"), exponent=Decimal("1"))
    fee = _curve_to_descriptor(curve)

    try:
        max_amount = max_buy_amount_under_collateral_cap(
            collateral_cap=collateral_cap,
            price=worst_price,
            fee=fee,
            money_step=money_step,
        )
    except FeeError:
        return FeeInclusiveSizeSkip(
            reason="cannot_size_under_fee_cap",
            worst_price=worst_price,
            collateral_cap=collateral_cap,
            min_valid_order_notional=min_valid_order_notional,
        )

    if desired_share_notional is not None:
        max_amount = min(max_amount, desired_share_notional)

    # Quantity from USDC amount at worst price; floor to qty_step.
    raw_qty = max_amount / worst_price
    steps = (raw_qty / qty_step).to_integral_value(rounding=ROUND_DOWN)
    quantity = steps * qty_step
    if quantity <= 0:
        return FeeInclusiveSizeSkip(
            reason="quantity_rounds_to_zero",
            worst_price=worst_price,
            collateral_cap=collateral_cap,
            min_valid_order_notional=min_valid_order_notional,
        )

    # Recalculate after rounding: share notional and fee.
    while quantity > 0:
        share_notional = (quantity * worst_price).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        )
        entry_fee = taker_fee_usdc_for_shares(
            shares=quantity, price=worst_price, fee=fee
        )
        debit = share_notional + entry_fee
        if debit <= collateral_cap:
            if min_valid_order_notional is not None and share_notional < min_valid_order_notional:
                # Try whether any size fits min + fee inside cap.
                return FeeInclusiveSizeSkip(
                    reason="venue_minimum_above_fee_inclusive_cap",
                    worst_price=worst_price,
                    collateral_cap=collateral_cap,
                    min_valid_order_notional=min_valid_order_notional,
                )
            return FeeInclusiveEntrySize(
                worst_price=worst_price,
                share_notional=share_notional,
                quantity=quantity,
                conservative_entry_fee=entry_fee,
                max_fee_inclusive_debit=debit,
                collateral_cap=collateral_cap,
                fee_curve=curve,
            )
        quantity -= qty_step

    return FeeInclusiveSizeSkip(
        reason="cannot_fit_after_rounding",
        worst_price=worst_price,
        collateral_cap=collateral_cap,
        min_valid_order_notional=min_valid_order_notional,
    )


def conservative_entry_fee_for(
    *, quantity: Decimal, price: Decimal, fee_curve: FeeCurveParams
) -> Decimal:
    fee = _curve_to_descriptor(fee_curve)
    return taker_fee_usdc_for_shares(shares=quantity, price=price, fee=fee)


def assert_fee_inclusive_debit(
    *,
    share_notional: Decimal,
    entry_fee: Decimal,
    cap: Decimal = Decimal("5.00"),
) -> None:
    if share_notional + entry_fee > cap:
        raise ValueError(
            f"fee-inclusive debit {share_notional + entry_fee} exceeds cap {cap}"
        )
