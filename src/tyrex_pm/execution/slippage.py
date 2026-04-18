from __future__ import annotations

from decimal import Decimal


def clamp_price(p: Decimal, *, tick: Decimal | None = None) -> Decimal:
    if tick is None or tick <= 0:
        return p
    return (p / tick).quantize(Decimal("1")) * tick
