"""RTDS ``activity`` / ``trades`` payload → activity-shaped row → ``GuruTradeSignal``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.core.types import GuruTradeSignal
from tyrex_pm.data.guru_parse import api_timestamp_to_ms, trade_row_to_signal


def normalize_wallet(addr: str | None) -> str:
    return (addr or "").strip().lower()


def rtds_payload_to_activity_row(payload: Mapping[str, Any]) -> dict[str, Any]:
    """
    Map RTDS trade payload keys to Data API–like row for :func:`trade_row_to_signal`.

    Handles camelCase and snake_case hints from Polymarket RTDS / REST examples.
    """

    def g(*keys: str) -> Any:
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
        return None

    ts = g("timestamp", "timeStamp")
    asset = g("asset", "assetId", "tokenId", "token_id")
    side = g("side")
    size = g("size")
    price = g("price")
    tx = g("transactionHash", "transaction_hash", "txHash", "tx_hash")
    slug = g("slug", "market_slug")
    event_slug = g("eventSlug", "event_slug")
    out: dict[str, Any] = {
        "type": "TRADE",
        "timestamp": ts,
        "asset": asset,
        "side": side,
        "size": size,
        "price": price,
        "transactionHash": tx,
        "slug": slug or event_slug,
        "eventSlug": event_slug,
    }
    return out


def rtds_trade_payload_to_signal(payload: Mapping[str, Any]) -> GuruTradeSignal | None:
    """Return normalized signal or None if row cannot be built."""

    row = rtds_payload_to_activity_row(payload)
    if row.get("asset") is None and row.get("side") is None:
        return None
    try:
        d = dict(row)
        d["timestamp"] = api_timestamp_to_ms(row.get("timestamp"))
        return trade_row_to_signal(d)
    except (TypeError, ValueError):
        return None


def guru_proxy_wallet_from_payload(payload: Mapping[str, Any]) -> str | None:
    v = (
        payload.get("proxyWallet")
        or payload.get("proxy_wallet")
        or payload.get("user")
        or payload.get("maker")
    )
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
