"""L1/Ln book imbalance and microprice on a generic DepthSnapshot."""

from __future__ import annotations

from decimal import Decimal
from math import log

from tyrex_pm.indicators.depth import DepthSnapshot


def _depth_qty(snapshot: DepthSnapshot, *, side: str, levels: int) -> Decimal:
    book = snapshot.bids if side == "bid" else snapshot.asks
    return sum((level.quantity for level in book[:levels]), Decimal("0"))


def imbalance(snapshot: DepthSnapshot, *, levels: int = 1) -> Decimal | None:
    """(Qb - Qa) / (Qb + Qa) over the first ``levels`` levels. None if empty."""
    if levels < 1:
        raise ValueError("levels must be >= 1")
    qb = _depth_qty(snapshot, side="bid", levels=levels)
    qa = _depth_qty(snapshot, side="ask", levels=levels)
    total = qb + qa
    if total <= 0:
        return None
    return (qb - qa) / total


def microprice(snapshot: DepthSnapshot) -> Decimal | None:
    bid = snapshot.best_bid
    ask = snapshot.best_ask
    if bid is None or ask is None:
        return None
    total = bid.quantity + ask.quantity
    if total <= 0:
        return None
    return (ask.price * bid.quantity + bid.price * ask.quantity) / total


def microprice_gap_bps(snapshot: DepthSnapshot) -> Decimal | None:
    """1e4 * log(microprice / mid). None if either side is missing."""
    mp = microprice(snapshot)
    mid = snapshot.mid
    if mp is None or mid is None or mid <= 0 or mp <= 0:
        return None
    return Decimal("10000") * Decimal(str(log(float(mp / mid))))
