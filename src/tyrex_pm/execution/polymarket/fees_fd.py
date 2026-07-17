"""CLOB V2 dynamic taker fees from ``/clob-markets`` ``fd`` (R7A.1).

Official formula (docs): ``fee = C × feeRate × p × (1 − p)`` with ``e`` as the
exponent on ``p(1−p)`` when present: ``fee = C × r × (p(1−p))**e``.

Sources:
- https://docs.polymarket.com/trading/fees
- ``GET /clob-markets/{condition_id}`` field ``fd``
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from typing import Any, Mapping


class FeeError(RuntimeError):
    pass


FEE_PARAMETERS_UNKNOWN = "FEE_PARAMETERS_UNKNOWN"


@dataclass(frozen=True)
class FeeDescriptor:
    fee_rate: Decimal
    exponent: Decimal
    taker_only: bool
    source: str
    condition_id: str | None = None

    @property
    def resolved(self) -> bool:
        return self.fee_rate >= 0 and self.exponent >= 0


def parse_fd(raw: Mapping[str, Any] | None, *, condition_id: str | None = None) -> FeeDescriptor:
    if not raw or not isinstance(raw, dict):
        raise FeeError(FEE_PARAMETERS_UNKNOWN)
    fd = raw.get("fd")
    if not isinstance(fd, dict):
        raise FeeError(FEE_PARAMETERS_UNKNOWN)
    try:
        r = Decimal(str(fd["r"]))
        e = Decimal(str(fd["e"]))
    except Exception as exc:  # noqa: BLE001
        raise FeeError(FEE_PARAMETERS_UNKNOWN) from exc
    to = bool(fd.get("to", True))
    return FeeDescriptor(
        fee_rate=r,
        exponent=e,
        taker_only=to,
        source="clob_markets_fd",
        condition_id=condition_id,
    )


def taker_fee_usdc_for_shares(
    *,
    shares: Decimal,
    price: Decimal,
    fee: FeeDescriptor,
) -> Decimal:
    """USDC taker fee for ``shares`` at ``price`` (probability units)."""
    if shares < 0:
        raise FeeError("negative_shares")
    if price < 0 or price > 1:
        raise FeeError("price_out_of_range")
    if shares == 0 or price in (0, 1):
        return Decimal("0")
    base = price * (Decimal("1") - price)
    if fee.exponent == fee.exponent.to_integral_value():
        phi = fee.fee_rate * (base ** int(fee.exponent))
    else:
        phi = fee.fee_rate * Decimal(str(float(base) ** float(fee.exponent)))
    # Official tables round fees to 5 decimal places.
    return (shares * phi).quantize(Decimal("0.00001"), rounding=ROUND_UP)


def taker_fee_usdc_for_buy_amount(
    *,
    buy_amount_usdc: Decimal,
    price: Decimal,
    fee: FeeDescriptor,
) -> Decimal:
    """Fee when BUY dollar amount ``A`` buys ``A/price`` shares at worst price."""
    if price <= 0:
        raise FeeError("price_out_of_range")
    shares = buy_amount_usdc / price
    return taker_fee_usdc_for_shares(shares=shares, price=price, fee=fee)


def max_buy_amount_under_collateral_cap(
    *,
    collateral_cap: Decimal,
    price: Decimal,
    fee: FeeDescriptor,
    money_step: Decimal = Decimal("0.01"),
) -> Decimal:
    """Largest FAK BUY ``amount`` such that amount + fee(amount) ≤ collateral_cap.

    Assumes FAK BUY ``amount`` is USDC spent on shares and taker fee is an
    *additional* USDC debit at match (conservative vs $5 envelope).
    """
    if collateral_cap <= 0 or price <= 0:
        raise FeeError("invalid_inputs")
    # fee = amount * r * (1-p)^e * p^(e-1); for e=1: fee = amount * r * (1-p)
    # Solve amount * (1 + k) <= cap where k = fee/amount
    one = Decimal("1")
    base = price * (one - price)
    if fee.exponent == fee.exponent.to_integral_value():
        phi = fee.fee_rate * (base ** int(fee.exponent))
    else:
        phi = fee.fee_rate * Decimal(str(float(base) ** float(fee.exponent)))
    # fee per USDC of amount at this price: (shares/amount)*phi = phi/price
    k = phi / price
    # amount + amount*k <= cap → amount <= cap/(1+k)
    if k < 0:
        raise FeeError(FEE_PARAMETERS_UNKNOWN)
    raw = collateral_cap / (one + k)
    steps = (raw / money_step).to_integral_value(rounding=ROUND_DOWN)
    amount = steps * money_step
    # Verify after rounding
    while amount > 0:
        fee_u = taker_fee_usdc_for_buy_amount(
            buy_amount_usdc=amount, price=price, fee=fee
        )
        if amount + fee_u <= collateral_cap:
            return amount
        amount -= money_step
    raise FeeError("cannot_size_under_fee_cap")
