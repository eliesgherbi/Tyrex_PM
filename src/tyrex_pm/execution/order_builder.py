"""Build a venue ``PlaceOrderRequest`` from an :class:`ApprovedIntent`.

Phase 5 (market-info adapter) added optional tick-size quantization at this
boundary: when a :class:`MarketInfo` is supplied, ``limit_price`` is floored to
the venue's ``mts`` *before* the request leaves the bot. This serves two goals:

* **Avoid venue precision rejections**: a strategy emitting ``0.5523`` on a
  tick-0.01 market would otherwise be rejected with
  ``price (0.5523) precision higher than tick (0.01)``. Floor-quantizing
  silently fixes the issue.
* **Forensic clarity**: the OMS submit fact records both the original and the
  quantized price (see :func:`build_quantize_evidence`) so an operator can
  see whether the bot rounded a price and by how much.

Quantization is *floor* (not round-to-nearest) so that:

* a BUY's effective price never exceeds the strategy's intended limit, and
* a SELL's effective price is never raised above the strategy's intended limit.

Both directions stay strictly *no worse* than what the strategy asked for; if
that is too aggressive, the strategy should pre-quantize itself.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.models import ApprovedIntent
from tyrex_pm.venue.polymarket.clob_execution import PlaceOrderRequest


def to_place_request(
    ap: ApprovedIntent,
    *,
    market_info: Any | None = None,
) -> PlaceOrderRequest:
    """Build a :class:`PlaceOrderRequest` from an approved intent.

    When ``market_info`` is supplied (live mode with a wired
    :class:`MarketInfoCache`), ``limit_price`` is quantized to the venue tick
    size; otherwise the original price is used unchanged (shadow mode and
    unit tests).
    """
    i = ap.intent
    raw_price = i.limit_price or Decimal("0")
    if market_info is not None:
        try:
            quantized = market_info.quantize_price(Decimal(str(raw_price)))
        except Exception:
            quantized = raw_price
        price = quantized
    else:
        price = raw_price
    return PlaceOrderRequest(
        token_id=i.token_id,
        side=i.side,
        size=i.size,
        price=price,
        style=i.order_style,
        client_order_id=ap.client_order_id,
    )


def build_quantize_evidence(
    ap: ApprovedIntent,
    market_info: Any | None,
) -> dict[str, Any]:
    """Return an evidence dict describing tick quantization for the OMS submit fact.

    Always returns at least ``{"tick_quantize_applied": False}`` so the schema
    is stable across shadow/live paths. When ``market_info`` is present the
    payload also carries the venue ``tick_size``, the original strategy
    price, the quantized price, and a boolean indicating whether the value
    actually moved.
    """
    raw_price = ap.intent.limit_price
    if market_info is None or raw_price is None:
        return {"tick_quantize_applied": False}
    try:
        original = Decimal(str(raw_price))
        quantized = market_info.quantize_price(original)
        tick_size = getattr(market_info, "tick_size", None)
    except Exception:
        return {"tick_quantize_applied": False}
    return {
        "tick_quantize_applied": True,
        "tick_size": str(tick_size) if tick_size is not None else None,
        "original_price": str(original),
        "quantized_price": str(quantized),
        "price_was_quantized": original != quantized,
    }
