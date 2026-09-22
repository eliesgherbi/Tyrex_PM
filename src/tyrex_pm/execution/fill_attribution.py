"""Owned fill identity shared by stream and REST reconciliation.

Venue TRADE/FILL payloads identify the taker at the trade level. A session that
was the maker must use the owned ``maker_orders[]`` clip, not taker size/side.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


def _value(model: Any, name: str, default: Any = None) -> Any:
    if isinstance(model, Mapping):
        return model.get(name, default)
    return getattr(model, name, default)


def _text(model: Any, *names: str) -> str:
    for name in names:
        raw = _value(model, name)
        if raw is not None and str(raw):
            return str(raw)
    return ""


def _decimal(raw: Any) -> Decimal | None:
    if raw is None or raw == "":
        return None
    return Decimal(str(raw))


@dataclass(frozen=True)
class OwnedFillClip:
    venue_order_id: str
    token_id: str
    side: str
    shares: Decimal
    price: Decimal
    attribution: str
    raw_maker_order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FillAttribution:
    clip: OwnedFillClip | None
    reason: str


def _maker_rows(payload: Any) -> tuple[Any, ...]:
    rows = _value(payload, "maker_orders") or _value(payload, "makerOrders") or ()
    if isinstance(rows, Mapping):
        return (rows,)
    return tuple(rows or ())


def attribute_owned_fill(
    payload: Any,
    owned_venue_ids: set[str],
    selected_token_id: str | None,
    *,
    owned_side_by_venue: Mapping[str, str] | None = None,
) -> FillAttribution:
    """Return the unique owned clip, or a fail-closed reason with no clip."""
    owned = {str(item) for item in owned_venue_ids if str(item)}
    taker_id = _text(payload, "taker_order_id", "takerOrderId", "order_id", "orderID")
    trade_token = _text(payload, "asset_id", "assetId", "token_id", "tokenId")
    trade_side = _text(payload, "side").upper() or "BUY"
    trade_size = _decimal(_value(payload, "size") or _value(payload, "matched_amount"))
    trade_price = _decimal(_value(payload, "price"))
    maker_ids: list[str] = []
    owned_makers: list[tuple[str, Any]] = []
    for row in _maker_rows(payload):
        maker_id = _text(row, "order_id", "orderID", "id")
        if not maker_id:
            continue
        maker_ids.append(maker_id)
        if maker_id in owned:
            owned_makers.append((maker_id, row))
    taker_owned = bool(taker_id) and taker_id in owned
    if taker_owned and owned_makers:
        return FillAttribution(None, "both_roles")
    if len(owned_makers) > 1:
        return FillAttribution(None, "multiple_owned_clips")
    if taker_owned:
        if selected_token_id and trade_token and trade_token != selected_token_id:
            return FillAttribution(None, "token_mismatch")
        if trade_size is None or trade_size <= 0 or trade_price is None or trade_price <= 0:
            return FillAttribution(None, "invalid_taker_fields")
        return FillAttribution(
            OwnedFillClip(
                venue_order_id=taker_id,
                token_id=trade_token or (selected_token_id or ""),
                side=trade_side,
                shares=trade_size,
                price=trade_price,
                attribution="taker",
                raw_maker_order_ids=tuple(maker_ids),
            ),
            "taker",
        )
    if len(owned_makers) == 1:
        maker_id, row = owned_makers[0]
        clip_size = _decimal(
            _value(row, "matched_amount")
            or _value(row, "matchedAmount")
            or _value(row, "size")
            or _value(row, "matched")
        )
        clip_price = _decimal(_value(row, "price") or trade_price)
        clip_token = _text(row, "asset_id", "assetId", "token_id", "tokenId") or trade_token
        if selected_token_id and clip_token and clip_token != selected_token_id:
            return FillAttribution(None, "token_mismatch")
        if clip_size is None or clip_size <= 0 or clip_price is None or clip_price <= 0:
            return FillAttribution(None, "invalid_maker_clip")
        sides = owned_side_by_venue or {}
        side = str(sides.get(maker_id) or "").upper() or trade_side
        return FillAttribution(
            OwnedFillClip(
                venue_order_id=maker_id,
                token_id=clip_token or (selected_token_id or ""),
                side=side,
                shares=clip_size,
                price=clip_price,
                attribution="maker_clip",
                raw_maker_order_ids=tuple(maker_ids),
            ),
            "maker_clip",
        )
    return FillAttribution(None, "unowned")


def is_known_normalization_defect(
    *,
    stored_venue_order_id: str,
    clip_venue_order_id: str,
    owned_venue_ids: set[str],
) -> bool:
    """Stored id is not owned; the recovered clip id is the session's maker/taker."""
    owned = {str(item) for item in owned_venue_ids if str(item)}
    stored = str(stored_venue_order_id or "")
    recovered = str(clip_venue_order_id or "")
    return bool(stored) and stored not in owned and recovered in owned
