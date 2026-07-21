"""Normalize Binance public trade stream payloads.

N2 timing contract:
- ``Event.ts_received`` = raw host wall UTC at ingress (never corrected).
- ``IngressMeta.receive_wall_corrected_utc`` = raw + clock offset when a
  TimeAuthorityView is supplied.
Trading reference only — never settlement truth.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.events import EventSource, ReferencePriceUpdated
from tyrex_pm.core.ids import CorrelationId, new_correlation_id, new_event_id
from tyrex_pm.core.ingress import FeedRole, IngressMeta, build_ingress_timing, fingerprint_payload
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
    time_view: Any | None = None,
    clock_snapshot_id: str | None = None,
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
        meta = build_ingress_timing(
            receive_wall_raw_utc=ts_received,
            receive_monotonic_ns=receive_monotonic_ns or 0,
            ingress_sequence=ingress_sequence,
            connection_generation=connection_generation,
            time_view=time_view,
            provider_sequence_id=None if trade_id is None else str(trade_id),
            raw_fingerprint=fingerprint_payload(data),
            late_or_out_of_order=ooo,
            role=FeedRole.TRADING_REFERENCE,
            subscription_mode="binance_spot_trade",
            clock_snapshot_id=clock_snapshot_id
            or (getattr(time_view, "clock_snapshot_id", None) if time_view else None),
        )
        if time_view is None and clock_uncertainty_ms is not None:
            # Preserve legacy uncertainty-only path without claiming correction.
            meta = IngressMeta(
                receive_monotonic_ns=meta.receive_monotonic_ns,
                ingress_sequence=meta.ingress_sequence,
                connection_generation=meta.connection_generation,
                receive_wall_raw_utc=meta.receive_wall_raw_utc,
                receive_wall_corrected_utc=meta.receive_wall_corrected_utc,
                clock_offset_ms=meta.clock_offset_ms,
                clock_uncertainty_ms=clock_uncertainty_ms,
                clock_status=meta.clock_status,
                clock_snapshot_id=meta.clock_snapshot_id,
                provider_sequence_id=meta.provider_sequence_id,
                raw_fingerprint=meta.raw_fingerprint,
                late_or_out_of_order=meta.late_or_out_of_order,
                role=meta.role,
                subscription_mode=meta.subscription_mode,
            )
    if meta is not None and meta.receive_wall_raw_utc is not None:
        if meta.receive_wall_raw_utc != ts_received:
            raise ValueError(
                "IngressMeta.receive_wall_raw_utc must equal Event.ts_received (raw wall)"
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
