"""Normalize Binance public trade stream payloads.

Feed choice (R3 validation):
- Stream: ``wss://stream.binance.com:9443/ws/<symbol>@trade``
  (individual trades; e.g. ``btcusdt@trade``).
- Event timestamp: trade time ``T`` (ms), not receive time.
- Sufficient for short-horizon momentum: dense prints give P_t samples
  without requiring account/ticker aggregation APIs.
- Reconnect: adapter reconnects and publishes; indicator may reset;
  reference store simply updates on next valid trade (no latched freshness).

N2: optional ``IngressMeta`` for corrected receive timing / connection generation.
Trading reference only — never settlement truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.events import EventSource, ReferencePriceUpdated
from tyrex_pm.core.ids import CorrelationId, new_correlation_id, new_event_id
from tyrex_pm.core.ingress import FeedRole, IngressMeta, fingerprint_payload
from tyrex_pm.core.snapshots import ReferencePriceSnapshot


def normalize_trade_message(
    payload: Mapping[str, Any],
    *,
    ts_received: datetime,
    correlation_id: CorrelationId | None = None,
    symbol: str | None = None,
    ingress: IngressMeta | None = None,
    receive_monotonic_ns: int | None = None,
    clock_uncertainty_ms: int | None = None,
    ingress_sequence: int | None = None,
    connection_generation: int | None = None,
    last_trade_id: int | None = None,
) -> ReferencePriceUpdated:
    # Combined streams wrap as {"stream": "...", "data": {...}}
    data = payload.get("data", payload)
    sym = str(symbol or data.get("s") or "").upper()
    if not sym:
        raise ValueError("trade message missing symbol")
    price = data.get("p") or data.get("price")
    if price is None:
        raise ValueError("trade message missing price")
    ts_ms = data.get("T") or data.get("E") or data.get("timestamp")
    if ts_ms is None:
        raise ValueError("trade message missing timestamp")
    ts_event = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
    trade_id = data.get("t")
    ooo = None
    if last_trade_id is not None and trade_id is not None and int(trade_id) < int(last_trade_id):
        ooo = "out_of_order"
    meta = ingress
    if meta is None and ingress_sequence is not None and connection_generation is not None:
        meta = IngressMeta(
            receive_monotonic_ns=receive_monotonic_ns or 0,
            clock_uncertainty_ms=clock_uncertainty_ms,
            ingress_sequence=ingress_sequence,
            connection_generation=connection_generation,
            provider_sequence_id=None if trade_id is None else str(trade_id),
            raw_fingerprint=fingerprint_payload(data),
            late_or_out_of_order=ooo,
            role=FeedRole.TRADING_REFERENCE,
            subscription_mode="binance_spot_trade",
        )
    snap = ReferencePriceSnapshot(
        symbol=sym,
        price=Decimal(str(price)),
        ts_event=ts_event,
        venue="binance",
    )
    return ReferencePriceUpdated(
        event_id=new_event_id(),
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=None,
        ts_event=ts_event,
        ts_received=ts_received,
        source=EventSource.BINANCE,
        reference=snap,
        ingress=meta,
    )
