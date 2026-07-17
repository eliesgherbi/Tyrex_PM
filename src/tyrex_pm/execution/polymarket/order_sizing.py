"""Conservative Decimal sizing under the $5 R7 BUY envelope (fee-aware)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from tyrex_pm.execution.polymarket.fees_fd import (
    FEE_PARAMETERS_UNKNOWN,
    FeeDescriptor,
    FeeError,
    max_buy_amount_under_collateral_cap,
    taker_fee_usdc_for_buy_amount,
)


class SizingError(RuntimeError):
    pass


BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP = "BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP"


@dataclass(frozen=True)
class SizedBuyOrder:
    """FAK market BUY: dollar ``amount`` + worst-price ``limit_price``.

    ``quantity`` is a *maximum estimated* share count at the worst price — not
    a guaranteed fill.
    """

    limit_price: Decimal
    quantity: Decimal
    amount: Decimal
    notional: Decimal
    estimated_buy_fee: Decimal
    max_collateral: Decimal
    order_type: str
    tick_size: Decimal
    min_order_size: Decimal
    reason: str


def _quantize_price(price: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        raise SizingError("tick_size must be > 0")
    steps = (price / tick).to_integral_value(rounding=ROUND_DOWN)
    return steps * tick


def _quantize_qty(qty: Decimal, step: Decimal = Decimal("0.01")) -> Decimal:
    if step <= 0:
        step = Decimal("0.01")
    steps = (qty / step).to_integral_value(rounding=ROUND_DOWN)
    return steps * step


def size_buy_under_cap(
    *,
    best_ask: Decimal,
    ask_size: Decimal,
    tick_size: Decimal,
    min_order_size: Decimal,
    max_buy_notional: Decimal = Decimal("5.00"),
    max_limit_price: Decimal | None = None,
    order_type: str = "FAK",
    qty_step: Decimal = Decimal("0.01"),
    money_step: Decimal = Decimal("0.01"),
    fee: FeeDescriptor | None = None,
) -> SizedBuyOrder:
    """Size a FAK BUY so order amount + max BUY fee ≤ collateral cap."""
    if max_buy_notional > Decimal("5.00"):
        raise SizingError("max_buy_notional exceeds $5 envelope")
    if fee is None:
        raise SizingError(FEE_PARAMETERS_UNKNOWN)
    if best_ask <= 0 or ask_size <= 0:
        raise SizingError("insufficient executable depth")
    cap_price = best_ask if max_limit_price is None else min(best_ask, max_limit_price)
    limit = _quantize_price(cap_price, tick_size)
    if limit <= 0:
        raise SizingError("limit_price quantized to zero")
    if max_limit_price is not None and limit > max_limit_price:
        raise SizingError("limit_price exceeds max_limit_price after tick quantize")

    try:
        amount = max_buy_amount_under_collateral_cap(
            collateral_cap=max_buy_notional,
            price=limit,
            fee=fee,
            money_step=money_step,
        )
    except FeeError as exc:
        raise SizingError(str(exc)) from exc

    # Cap by visible depth notional
    depth_notional = _quantize_qty(ask_size, qty_step) * limit
    depth_money = (depth_notional / money_step).to_integral_value(rounding=ROUND_DOWN) * money_step
    amount = min(amount, depth_money)
    if amount <= 0:
        raise SizingError("zero_notional")

    max_shares = _quantize_qty(amount / limit, qty_step)
    max_shares = min(max_shares, _quantize_qty(ask_size, qty_step))
    if max_shares < min_order_size:
        min_amount = (min_order_size * limit).quantize(money_step, rounding=ROUND_DOWN)
        min_fee = taker_fee_usdc_for_buy_amount(
            buy_amount_usdc=min_amount, price=limit, fee=fee
        )
        if min_amount + min_fee > max_buy_notional:
            raise SizingError(BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP)
        raise SizingError("insufficient_depth_for_min_order_size")

    buy_fee = taker_fee_usdc_for_buy_amount(
        buy_amount_usdc=amount, price=limit, fee=fee
    )
    if amount + buy_fee > max_buy_notional:
        raise SizingError(BLOCKED_MINIMUM_ORDER_EXCEEDS_AUTHORIZED_CAP)

    return SizedBuyOrder(
        limit_price=limit,
        quantity=max_shares,
        amount=amount,
        notional=amount,
        estimated_buy_fee=buy_fee,
        max_collateral=amount + buy_fee,
        order_type=order_type,
        tick_size=tick_size,
        min_order_size=min_order_size,
        reason="SIZED_UNDER_CAP_FAK_AMOUNT_PLUS_FEE",
    )
