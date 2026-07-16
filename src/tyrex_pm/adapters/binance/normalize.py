"""Normalize Binance public trade stream payloads.

Feed choice (R3 validation):
- Stream: ``wss://stream.binance.com:9443/ws/<symbol>@trade``
  (individual trades; e.g. ``btcusdt@trade``).
- Event timestamp: trade time ``T`` (ms), not receive time.
- Sufficient for short-horizon momentum: dense prints give P_t samples
  without requiring account/ticker aggregation APIs.
- Reconnect: adapter reconnects and publishes; indicator may reset;
  reference store simply updates on next valid trade (no latched freshness).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.events import EventSource, ReferencePriceUpdated
from tyrex_pm.core.ids import CorrelationId, new_correlation_id, new_event_id
from tyrex_pm.core.snapshots import ReferencePriceSnapshot


def normalize_trade_message(
    payload: Mapping[str, Any],
    *,
    ts_received: datetime,
    correlation_id: CorrelationId | None = None,
    symbol: str | None = None,
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
    )
