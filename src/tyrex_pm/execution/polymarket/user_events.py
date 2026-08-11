"""Normalize authenticated Polymarket stream messages."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Mapping, TypeAlias


@dataclass(frozen=True, kw_only=True)
class UserStreamOrderEvidence:
    kind: Literal["order"] = "order"
    venue_order_id: str | None
    client_order_id: str | None
    status: str | None
    side: str | None
    instrument_token_id: str | None
    original_size: Decimal | None
    cumulative_matched_qty: Decimal
    trade_ids: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class UserStreamTradeEvidence:
    kind: Literal["trade"] = "trade"
    venue_trade_id: str
    venue_order_id: str | None
    client_order_id: str | None
    status: str
    side: str
    instrument_token_id: str
    size: Decimal
    price: Decimal
    cumulative_matched_qty: Decimal | None


UserStreamEvidence: TypeAlias = UserStreamOrderEvidence | UserStreamTradeEvidence


def _decimal(raw: Any, default: str = "0") -> Decimal:
    return Decimal(default) if raw is None or raw == "" else Decimal(str(raw))


def _model_mapping(model: Any) -> dict[str, Any]:
    if isinstance(model, Mapping):
        return dict(model)
    dump = getattr(model, "model_dump", None)
    if callable(dump):
        return dict(dump(by_alias=True, mode="json"))
    return {}


def _flatten(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    event_type = str(
        payload.get("event_type") or payload.get("type") or payload.get("eventType") or ""
    ).upper()
    nested = payload.get("payload")
    if nested is not None:
        mapped = _model_mapping(nested)
        if mapped:
            return event_type, mapped
    for key in ("order", "trade", "data"):
        mapped = _model_mapping(payload.get(key))
        if mapped:
            nested_type = str(mapped.get("type") or event_type).upper()
            return nested_type, mapped
    return event_type, dict(payload)


def _order_id(payload: Mapping[str, Any]) -> str | None:
    value = payload.get("orderID") or payload.get("order_id") or payload.get("id")
    return None if value is None or not str(value) else str(value)


def normalize_user_stream_message(
    payload: Mapping[str, Any],
) -> UserStreamEvidence | None:
    if not isinstance(payload, Mapping):
        return None
    event_type, row = _flatten(payload)
    if event_type in {"ORDER", "UPDATE", "PLACEMENT", "CANCELLATION", "ORDER_UPDATE"}:
        trades_raw = (
            row.get("trade_ids") or row.get("tradeIDs") or row.get("associate_trades") or ()
        )
        trade_ids = (
            (str(trades_raw),)
            if isinstance(trades_raw, str) and trades_raw
            else tuple(str(value) for value in trades_raw if str(value))
        )
        original = row.get("original_size") or row.get("originalSize") or row.get("size")
        token = row.get("asset_id") or row.get("assetId") or row.get("token_id")
        client = row.get("client_order_id") or row.get("clientOrderId")
        return UserStreamOrderEvidence(
            venue_order_id=_order_id(row),
            client_order_id=None if client is None else str(client),
            status=None if row.get("status") is None else str(row.get("status")),
            side=None if row.get("side") is None else str(row.get("side")).upper(),
            instrument_token_id=None if token is None else str(token),
            original_size=None if original is None else _decimal(original),
            cumulative_matched_qty=_decimal(
                row.get("size_matched")
                or row.get("sizeMatched")
                or row.get("matched_amount")
                or row.get("matchedAmount")
            ),
            trade_ids=trade_ids,
        )
    if event_type in {"TRADE", "FILL"}:
        trade_id = row.get("id") or row.get("trade_id") or row.get("tradeID")
        if trade_id is None or not str(trade_id):
            return None
        venue_order = row.get("taker_order_id") or row.get("order_id") or row.get("maker_order_id")
        token = row.get("asset_id") or row.get("assetId") or row.get("token_id")
        client = row.get("client_order_id") or row.get("clientOrderId")
        cumulative = row.get("size_matched") or row.get("sizeMatched")
        return UserStreamTradeEvidence(
            venue_trade_id=str(trade_id),
            venue_order_id=None if venue_order is None else str(venue_order),
            client_order_id=None if client is None else str(client),
            status=str(row.get("status") or "MATCHED").removeprefix("TRADE_STATUS_").upper(),
            side=str(row.get("side") or "BUY").upper(),
            instrument_token_id="" if token is None else str(token),
            size=_decimal(row.get("size") or row.get("matched_amount") or row.get("matchedAmount")),
            price=_decimal(row.get("price")),
            cumulative_matched_qty=None if cumulative is None else _decimal(cumulative),
        )
    # Official payloads occasionally omit the envelope type.
    if row.get("size_matched") is not None or row.get("sizeMatched") is not None:
        return normalize_user_stream_message({"type": "ORDER", "payload": row})
    if row.get("taker_order_id") is not None and row.get("price") is not None:
        return normalize_user_stream_message({"type": "TRADE", "payload": row})
    return None
