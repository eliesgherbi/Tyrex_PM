"""The one taker fill rule for simulated venues: the replay engine and the paper venue.

A taker order walks the opposite side of the book from the best price, never past its
limit. Each level fills at most ``participation`` of its displayed size, minus what earlier
orders already took from it in the same book epoch (``consumed``). A friction penalty
(``adverse``) makes every fill pay worse than the displayed level, never beyond the limit.

Two order shapes exist, as on the venue:

* by **shares** (SELL, or a share-sized BUY): fill up to ``quantity`` shares;
* by **spend** (amount BUY): spend up to ``spend`` collateral; the shares bought at each
  level are rounded down to ``share_quantum``.

Taker fees follow the venue curve per share and are rounded half-up to ``fee_quantum``.
A BUY whose fee is charged in shares receives ``shares - fee / price``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from tyrex_pm.domain.polymarket.fees import FeeCurveParams, phi_taker_fee_per_share

ZERO = Decimal(0)
SHARE_QUANTUM = Decimal("0.01")
FEE_QUANTUM = Decimal("0.00001")


@dataclass(frozen=True)
class LevelFill:
    level_price: Decimal  # displayed price of the level
    price: Decimal  # price paid or received (after the friction penalty)
    shares: Decimal


def eligible_prices(
    levels: Mapping[Decimal, Decimal], *, side: str, limit: Decimal, adverse: Decimal = ZERO
) -> list[Decimal]:
    """Levels a taker order may hit, best first."""
    if side == "BUY":
        return [p for p in sorted(levels) if p + adverse <= limit]
    return [p for p in sorted(levels, reverse=True) if p - adverse >= limit]


def level_capacity(
    levels: Mapping[Decimal, Decimal],
    price: Decimal,
    *,
    participation: Decimal = Decimal(1),
    consumed: Callable[[Decimal], Decimal] | None = None,
) -> Decimal:
    taken = ZERO if consumed is None else consumed(price)
    return max(ZERO, levels[price] * participation - taken)


def walk_shares(
    levels: Mapping[Decimal, Decimal],
    *,
    side: str,
    quantity: Decimal,
    limit: Decimal,
    participation: Decimal = Decimal(1),
    adverse: Decimal = ZERO,
    consumed: Callable[[Decimal], Decimal] | None = None,
) -> list[LevelFill]:
    """Fill up to ``quantity`` shares, best level first."""
    fills: list[LevelFill] = []
    remaining = quantity
    for price in eligible_prices(levels, side=side, limit=limit, adverse=adverse):
        take = min(
            remaining,
            level_capacity(levels, price, participation=participation, consumed=consumed),
        )
        if take:
            paid = price + adverse if side == "BUY" else price - adverse
            fills.append(LevelFill(price, paid, take))
            remaining -= take
    return fills


def walk_spend(
    levels: Mapping[Decimal, Decimal],
    *,
    spend: Decimal,
    limit: Decimal,
    participation: Decimal = Decimal(1),
    adverse: Decimal = ZERO,
    share_quantum: Decimal = SHARE_QUANTUM,
    consumed: Callable[[Decimal], Decimal] | None = None,
) -> tuple[list[LevelFill], Decimal]:
    """Amount BUY: spend up to ``spend`` on the asks. Returns the fills and unspent cash."""
    fills: list[LevelFill] = []
    remaining = spend
    for price in eligible_prices(levels, side="BUY", limit=limit, adverse=adverse):
        capacity = level_capacity(levels, price, participation=participation, consumed=consumed)
        paid = price + adverse
        take = min(capacity, remaining / paid)
        take = (take / share_quantum).to_integral_value(rounding=ROUND_DOWN) * share_quantum
        if take <= 0:
            continue
        fills.append(LevelFill(price, paid, take))
        remaining -= take * paid
        if remaining < share_quantum * paid:
            break
    return fills, remaining


def taker_fee(
    shares: Decimal, price: Decimal, curve: FeeCurveParams, *, quantum: Decimal = FEE_QUANTUM
) -> Decimal:
    fee = shares * phi_taker_fee_per_share(price, curve)
    return (fee / quantum).quantize(Decimal(1), rounding=ROUND_HALF_UP) * quantum


def bought_shares(shares: Decimal, price: Decimal, fee: Decimal) -> Decimal:
    """Shares received by a BUY whose fee is charged in shares."""
    return shares - fee / price
