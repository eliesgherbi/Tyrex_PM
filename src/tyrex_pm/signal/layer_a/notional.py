"""Guru trade notional (USD) for Layer A significance filters."""

from __future__ import annotations

from tyrex_pm.core.types import GuruTradeSignal


def notional_usd(sig: GuruTradeSignal) -> float | None:
    """
    ``price_raw * size_raw`` when both present and strictly positive after float coercion.

    Returns ``None`` when components missing or non-positive.
    """
    if sig.price_raw is None or sig.size_raw is None:
        return None
    try:
        px = float(sig.price_raw)
        sz = float(sig.size_raw)
    except (TypeError, ValueError):
        return None
    if px <= 0.0 or sz <= 0.0:
        return None
    return px * sz
