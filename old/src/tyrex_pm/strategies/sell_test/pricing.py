"""Auto-pricing helpers for the standalone sell_test strategy.

These helpers translate "I want a marketable BUY/SELL on this token" into a
concrete tick-aligned ``limit_price`` derived from the venue order book. They
are intentionally pure (book in -> price out) so the bulk of the logic is
unit-testable without any live venue dependency.

Only the thin :func:`resolve_marketable_price_via_client` wrapper actually
calls the V2 SDK (``ClobClient.get_order_book``); everything else operates on
the parsed book payload.

The strategy uses these via two integration points:

* :func:`tyrex_pm.runtime.app._run_sell_test_loop` — resolves the BUY price
  before the first ``EnterIntent`` is built.
* :func:`tyrex_pm.strategies.sell_test.strategy.SellTestStrategy.resolve_due_work_units`
  — resolves each SELL price right before the ``ExitIntent`` work unit is
  emitted, so the price reflects the book at SELL time, not at BUY time.

Pricing rules
~~~~~~~~~~~~~
* BUY: ``best_ask + aggression_ticks * tick_size``, clamped to the tradeable
  range ``[tick, 1 - tick]``, then floor-quantized to the tick grid (matches
  :meth:`tyrex_pm.venue.polymarket.market_info.MarketInfo.quantize_price`).
  Falling back to the configured ``limit_price`` is the only failure mode;
  there is no notion of "skip the BUY" because the strategy's contract is to
  fire one cycle.
* SELL: ``best_bid - aggression_ticks * tick_size``, with the same clamp and
  quantization. Aggression on the SELL means *crossing the spread downward*,
  i.e. selling into the bid.

Safety guardrails
~~~~~~~~~~~~~~~~~
* ``max_price`` (BUY only) refuses to pay more than the operator-set ceiling.
* ``min_price`` (SELL only) refuses to sell below the operator-set floor.

Both guardrails fall back to the configured fallback price (or the configured
``limit_price`` when the strategy was launched in fixed mode but later forced
through this helper, which is not the current call pattern but kept for
defensive symmetry).
"""
 
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)


# Default tick used when no MarketInfo is available (e.g. shadow mode without
# a live cache). Matches Polymarket's most common tick across binary markets.
_DEFAULT_TICK = Decimal("0.01")


@dataclass(frozen=True)
class ResolvedPrice:
    """Outcome of an auto-pricing resolution attempt.

    ``source``:

    * ``auto_book`` — best price came from a fresh venue order book.
    * ``fallback`` — the venue lookup failed or a guardrail tripped; ``price``
      is the configured fallback (or ``Decimal("0")`` when no fallback was
      provided, which the caller must validate before submitting).

    All fields are populated even on fallback so the emitted fact contains a
    complete forensic record of *why* the fallback fired.
    """

    price: Decimal
    source: str
    best_ask: Decimal | None
    best_bid: Decimal | None
    tick_size: Decimal
    aggression_ticks: int
    error: str | None

    def to_evidence(self) -> dict[str, Any]:
        """Serialize for inclusion in a fact payload (``str`` everywhere)."""
        return {
            "price": str(self.price),
            "source": self.source,
            "best_ask": str(self.best_ask) if self.best_ask is not None else None,
            "best_bid": str(self.best_bid) if self.best_bid is not None else None,
            "tick_size": str(self.tick_size),
            "aggression_ticks": int(self.aggression_ticks),
            "error": self.error,
        }


def _parse_levels(book: Any, key: str) -> list[Decimal]:
    """Extract sorted-ascending price levels from a Polymarket book payload.

    Polymarket's REST book response is ``{"bids": [{"price": ..., "size": ...}],
    "asks": [...]}`` where each entry has positive ``size`` while resting.
    Levels with ``size == 0`` are stale (the venue sometimes echoes them) and
    are filtered out so they cannot pollute the best-of-book picks.
    """
    levels = book.get(key) if isinstance(book, dict) else None
    if not levels or not isinstance(levels, list):
        return []
    out: list[Decimal] = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        try:
            price = Decimal(str(level.get("price")))
            size = Decimal(str(level.get("size", "0")))
        except Exception:  # noqa: BLE001
            continue
        if size > 0:
            out.append(price)
    return out


def best_levels_from_book(book: Any) -> tuple[Decimal | None, Decimal | None]:
    """Return ``(best_bid, best_ask)`` from a Polymarket book payload.

    "Best bid" is the highest non-zero bid price, "best ask" is the lowest
    non-zero ask price. Returns ``(None, None)`` when either side is empty so
    callers can fall back deterministically.
    """
    bids = _parse_levels(book, "bids")
    asks = _parse_levels(book, "asks")
    best_bid = max(bids, default=None)
    best_ask = min(asks, default=None)
    return best_bid, best_ask


def _quantize_floor(price: Decimal, tick: Decimal) -> Decimal:
    """Round ``price`` down to the nearest multiple of ``tick``.

    Mirrors :meth:`MarketInfo.quantize_price` for the same reason: rounding
    *down* never pushes a BUY past the operator-set ceiling and is symmetric
    enough on the SELL side that we keep one rule everywhere.
    """
    if tick <= 0:
        return price
    ticks = (price / tick).to_integral_value(rounding="ROUND_DOWN")
    return (ticks * tick).normalize()


def _clamp_to_tradeable(price: Decimal, tick: Decimal) -> Decimal:
    """Clamp to ``[tick, 1 - tick]`` so the venue does not reject ``0`` / ``1``.

    Polymarket rejects strictly-zero or strictly-one prices; the tradeable
    interior is ``(0, 1)``. Snapping to the closest in-bounds tick is the
    safest sane behavior for an auto-pricer that trips a venue edge.
    """
    if tick <= 0:
        return price
    lo = tick
    hi = Decimal("1") - tick
    if price < lo:
        return lo
    if price > hi:
        return hi
    return price


def compute_marketable_price(
    *,
    side: str,
    best_bid: Decimal | None,
    best_ask: Decimal | None,
    tick_size: Decimal,
    aggression_ticks: int,
    fallback_price: Decimal | None,
    max_price: Decimal | None = None,
    min_price: Decimal | None = None,
) -> ResolvedPrice:
    """Pure pricing function: given a parsed book, compute the marketable price.

    Separated from the async venue call so the bulk of the logic is testable
    with hand-built ``(best_bid, best_ask)`` tuples and no SDK on the path.
    """
    side_u = side.upper()
    tick = tick_size if tick_size > 0 else _DEFAULT_TICK
    fb = fallback_price if fallback_price is not None else Decimal("0")

    def _fb(reason: str) -> ResolvedPrice:
        return ResolvedPrice(
            price=fb,
            source="fallback",
            best_ask=best_ask,
            best_bid=best_bid,
            tick_size=tick,
            aggression_ticks=aggression_ticks,
            error=reason,
        )

    if side_u == "BUY":
        if best_ask is None:
            return _fb("no_asks_on_book")
        candidate = best_ask + (Decimal(aggression_ticks) * tick)
        if max_price is not None and candidate > max_price:
            return _fb(f"candidate_{candidate}_exceeds_max_price_{max_price}")
    elif side_u == "SELL":
        if best_bid is None:
            return _fb("no_bids_on_book")
        candidate = best_bid - (Decimal(aggression_ticks) * tick)
        if min_price is not None and candidate < min_price:
            return _fb(f"candidate_{candidate}_below_min_price_{min_price}")
    else:
        return _fb(f"unknown_side_{side}")

    snapped = _clamp_to_tradeable(candidate, tick)
    quantized = _quantize_floor(snapped, tick)
    if quantized <= 0:
        return _fb(f"quantized_price_{quantized}_not_positive")
    return ResolvedPrice(
        price=quantized,
        source="auto_book",
        best_ask=best_ask,
        best_bid=best_bid,
        tick_size=tick,
        aggression_ticks=aggression_ticks,
        error=None,
    )


async def fetch_order_book(client: Any, token_id: str) -> Any:
    """Wrap the synchronous V2 SDK call so it does not block the event loop.

    Mirrors the threading pattern used by
    :class:`tyrex_pm.venue.polymarket.market_info.MarketInfoCache` for
    ``get_neg_risk`` / ``get_fee_rate_bps``.
    """
    return await asyncio.to_thread(client.get_order_book, str(token_id))


async def resolve_marketable_price_via_client(
    *,
    client: Any,
    market_info: Any,
    token_id: str,
    side: str,
    aggression_ticks: int,
    fallback_price: Decimal | None,
    max_price: Decimal | None = None,
    min_price: Decimal | None = None,
) -> ResolvedPrice:
    """Fetch the venue book and return a :class:`ResolvedPrice`.

    Any exception during the venue lookup (network error, HTTP 5xx, parse
    failure) is converted to a fallback :class:`ResolvedPrice` with a
    descriptive ``error`` so the caller can keep going and emit a fact
    instead of crashing the run.
    """
    tick = (
        market_info.tick_size
        if market_info is not None and getattr(market_info, "tick_size", None) is not None
        else _DEFAULT_TICK
    )
    try:
        book = await fetch_order_book(client, token_id)
    except Exception as e:  # noqa: BLE001 — defensive: fall back rather than crash
        log.warning("sell_test pricing: get_order_book(%s) failed: %r", token_id, e)
        return ResolvedPrice(
            price=fallback_price if fallback_price is not None else Decimal("0"),
            source="fallback",
            best_ask=None,
            best_bid=None,
            tick_size=tick,
            aggression_ticks=aggression_ticks,
            error=f"book_fetch_failed: {e!r}",
        )
    best_bid, best_ask = best_levels_from_book(book)
    return compute_marketable_price(
        side=side,
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=tick,
        aggression_ticks=aggression_ticks,
        fallback_price=fallback_price,
        max_price=max_price,
        min_price=min_price,
    )
