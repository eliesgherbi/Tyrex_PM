"""Instrument grid for limit orders (technical, not operator policy).

Snap price to tick and quantity to ``size_increment``. Never increases quantity toward
``min_quantity``: if the stepped qty is below venue ``min_quantity``, submit is aborted here
so Tyrex does not silently exceed risk-approved size. Business min/max USD is **risk** only.
"""

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


def quantize_limit_order_for_instrument(
    inst: Any,
    *,
    side: str,
    price: float,
    quantity: float,
) -> NormalizeResult:
    """
    Round price to tick; floor quantity to size step. Does **not** bump up to ``min_quantity``.

    Fails when the grid-fit quantity is non-positive or strictly below ``min_quantity`` (venue would
    reject or would require increasing size past the risk-approved ``quantity``).
    """
    qty_in = float(quantity)
    if qty_in <= 0:
        return NormalizeResult(False, float(price), qty_in, "non_positive_qty")
    tick = max(_tick(inst), 1e-12)
    step = max(_size_step(inst), 1e-12)
    min_q = max(_min_qty(inst), 0.0)

    if min_q > 0 and min_q - 1e-12 > qty_in:
        return NormalizeResult(
            False,
            float(price),
            qty_in,
            f"min_quantity={min_q} exceeds risk_qty={qty_in}",
        )

    dp = Decimal(str(price))
    dt = Decimal(str(tick))
    n_ticks = (dp / dt).to_integral_value(rounding=ROUND_DOWN)
    adj_p = float(n_ticks * dt)
    side_u = side.upper()
    if side_u == "SELL":
        n2 = math.ceil(float(dp / dt) - 1e-15)
        adj_p = float(Decimal(n2) * dt)

    n_steps = math.floor(qty_in / step + 1e-12)
    adj_q = n_steps * step
    adj_q = min(adj_q, qty_in)
    if adj_q <= 0:
        return NormalizeResult(False, adj_p, adj_q, "qty_rounded_to_zero")

    if min_q > 0 and adj_q + 1e-12 < min_q:
        return NormalizeResult(
            False,
            adj_p,
            adj_q,
            f"below_min_quantity_after_step floored={adj_q} min_q={min_q}",
        )

    return NormalizeResult(True, adj_p, adj_q, "ok")


def floor_quantity_to_step(inst: Any, qty: float, approved_quantity: float) -> float:
    """Floor ``qty`` to instrument size step, cap at ``approved_quantity`` (no upward bump)."""
    step = max(_size_step(inst), 1e-12)
    n_steps = math.floor(float(qty) / step + 1e-12)
    return min(n_steps * step, float(approved_quantity))
