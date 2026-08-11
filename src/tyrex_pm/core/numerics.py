"""Decimal helpers for trading values.

Internal representation
-----------------------
Prices, quantities, notionals, tick sizes, and minimum sizes use
``decimal.Decimal``. Binary floats are rejected at construction boundaries.

Rounding ownership
------------------
R2 validates domain ranges and sign/non-negativity only. Venue tick / min-size
rounding and quantization are **not** implemented here; they belong to the
Polymarket adapter / execution boundary (R3–R6) where market metadata is known.

Indicators may use float math internally in R3+, but must convert explicitly
when publishing Decimal fields on snapshots or intents.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TypeAlias

DecimalLike: TypeAlias = Decimal | str | int


class NumericError(ValueError):
    """Invalid trading numeric."""


def as_decimal(value: DecimalLike, *, field_name: str) -> Decimal:
    if isinstance(value, float):
        raise NumericError(f"{field_name} must not use binary float; pass Decimal or str")
    try:
        if isinstance(value, Decimal):
            result = value
        else:
            result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise NumericError(f"{field_name} is not a valid Decimal: {value!r}") from exc
    if not result.is_finite():
        raise NumericError(f"{field_name} must be finite, got {result}")
    return result


def require_non_negative(value: Decimal, *, field_name: str) -> Decimal:
    if value < 0:
        raise NumericError(f"{field_name} must be >= 0, got {value}")
    return value


def require_polymarket_price(value: Decimal, *, field_name: str = "price") -> Decimal:
    """Polymarket outcome prices are probabilities in [0, 1]."""
    if value < 0 or value > 1:
        raise NumericError(f"{field_name} must be in [0, 1], got {value}")
    return value
