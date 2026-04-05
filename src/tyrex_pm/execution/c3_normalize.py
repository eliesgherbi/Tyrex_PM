"""C3 venue normalization: tick / size step / min-notional without qty above risk intent."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class NormalizeResult:
    ok: bool
    price: float
    quantity: float
    detail: str = ""


def _tick(inst: Any) -> float:
    inc = getattr(inst, "price_increment", None)
    if inc is None:
        return 0.01
    try:
        return float(inc)
    except (TypeError, ValueError):
        return float(getattr(inc, "raw", inc))


def _size_step(inst: Any) -> float:
    inc = getattr(inst, "size_increment", None)
    if inc is None:
        return 1.0
    try:
        return float(inc)
    except (TypeError, ValueError):
        return float(getattr(inc, "raw", inc))


def _min_qty(inst: Any) -> float:
    mq = getattr(inst, "min_quantity", None)
    if mq is None:
        return 0.0
    try:
        return float(mq)
    except (TypeError, ValueError):
        return float(getattr(mq, "raw", mq))


def normalize_venue_submit(
    inst: Any,
    *,
    side: str,
    price: float,
    quantity: float,
    approved_quantity: float,
    min_buy_notional_usd: float,
) -> NormalizeResult:
    """
    Round price to tick (down for BUY, up for SELL in tick units — conservative limit placement).

    Floor quantity to size step and ``min_quantity``. Never exceed ``approved_quantity``.
    If BUY min-notional cannot be met at or below approved qty → fail.
    """
    if approved_quantity <= 0 or quantity <= 0:
        return NormalizeResult(False, price, quantity, "non_positive_qty")
    qty = min(float(quantity), float(approved_quantity))
    tick = max(_tick(inst), 1e-12)
    step = max(_size_step(inst), 1e-12)
    min_q = max(_min_qty(inst), 0.0)

    # Price — quantize to tick grid toward passive side (BUY: lower price, SELL: higher)
    dp = Decimal(str(price))
    dt = Decimal(str(tick))
    n_ticks = (dp / dt).to_integral_value(rounding=ROUND_DOWN)
    adj_p = float(n_ticks * dt)
    side_u = side.upper()
    if side_u == "SELL":
        # round up to tick for sell limit conservatism (do not sell below grid)
        n2 = math.ceil(float(dp / dt) - 1e-15)
        adj_p = float(Decimal(n2) * dt)

    # Quantity — floor to step, cap to approved
    n_steps = math.floor(qty / step + 1e-12)
    adj_q = n_steps * step
    if min_q > 0 and adj_q + 1e-12 < min_q:
        # cannot satisfy min lot without raising qty — only allow if min_q still <= approved
        if min_q - 1e-12 > approved_quantity:
            return NormalizeResult(
                False,
                adj_p,
                qty,
                f"min_quantity={min_q} exceeds approved_qty={approved_quantity}",
            )
        adj_q = min_q
    adj_q = min(adj_q, approved_quantity)
    if adj_q <= 0:
        return NormalizeResult(False, adj_p, adj_q, "qty_rounded_to_zero")

    if side_u == "BUY" and min_buy_notional_usd > 0:
        if adj_p * adj_q + 1e-9 < min_buy_notional_usd:
            # would need larger qty — forbidden
            return NormalizeResult(
                False,
                adj_p,
                adj_q,
                f"min_notional not met est={adj_p * adj_q} min={min_buy_notional_usd}",
            )

    if adj_q + 1e-12 < min_q:
        return NormalizeResult(
            False,
            adj_p,
            adj_q,
            f"below min_quantity after floor min_q={min_q}",
        )

    return NormalizeResult(True, adj_p, adj_q, "ok")


def floor_quantity_to_step(inst: Any, qty: float, approved_quantity: float) -> float:
    """Floor ``qty`` to instrument size step, cap at ``approved_quantity`` (no upward bump)."""
    step = max(_size_step(inst), 1e-12)
    n_steps = math.floor(float(qty) / step + 1e-12)
    return min(n_steps * step, float(approved_quantity))
