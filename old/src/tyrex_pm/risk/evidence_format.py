"""Shared formatting helpers for risk evidence (deployment, capital, in-flight).

Why this exists
---------------
Decimal arithmetic on user-WS / REST inputs produces noisy tails like
``"4.000000000000000000000000002"`` in ``risk_decision`` facts. Operators reading
``facts.jsonl`` cannot ``grep`` or ``diff`` cleanly when the same logical USD figure
renders as two different strings depending on the evaluation path.

This module standardizes the *display* precision (logic still uses full Decimal). All
risk evidence USD numbers should go through :func:`q_usd` before being converted to ``str``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from typing import Any

#: Display precision for USD numbers in risk_decision / wallet_sync facts. 6 decimals is
#: comfortably below micro-USDC and matches Polymarket's USDC fee rounding convention.
_USD_QUANT = Decimal("0.000001")

#: Local Decimal context with enough precision (40 digits) to quantize the very large
#: synthetic allowance fallback (``10**30``, see ``clob_wallet_sync._sync_wallet_from_clob``)
#: down to 6-decimal display precision without raising ``InvalidOperation``.
#: 36 integer digits + 6 fractional digits = 42 → 40 leaves room for full integer part of
#: every realistic USDC figure. The default 28-digit context only covers up to ~10**22.
_USD_CTX = Context(prec=40)


def q_usd(value: Decimal | int | float | str) -> Decimal:
    """Quantize a USD figure to 6 decimal places using bankers' rounding.

    Accepts any input ``Decimal`` accepts. Non-Decimal inputs are coerced once. Returns a
    ``Decimal`` so callers can either ``str()`` it for facts or keep using it as a number.

    Falls back to integer-rounded (or unchanged) Decimal if the value is so large that it
    cannot fit in the local 40-digit context — keeps the gate observability-only and never
    crashes the live runtime on a pathological input.
    """
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    try:
        return d.quantize(_USD_QUANT, rounding=ROUND_HALF_EVEN, context=_USD_CTX)
    except InvalidOperation:
        # Value exceeds even the 40-digit context (extreme synthetic allowance like 10**38);
        # round to the nearest whole USD instead of crashing the trading loop.
        try:
            return d.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN, context=_USD_CTX)
        except InvalidOperation:
            return d


def s_usd(value: Decimal | int | float | str | None) -> str | None:
    """``str(q_usd(value))`` with ``None`` passthrough — convenience for evidence dicts."""
    if value is None:
        return None
    return str(q_usd(value))


def s_usd_map(mapping: dict[Any, Decimal | int | float | str]) -> dict[str, str]:
    """Quantize every value of a per-token / per-bucket USD map; keys coerced to str."""
    return {str(k): str(q_usd(v)) for k, v in mapping.items()}
