"""Normalize Polymarket RTDS crypto price payloads (N2).

No Z-Gap thresholds or PTB selection — ticks only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.events import (
    EventSource,
    ReferencePriceUpdated,
    SettlementReferenceUpdated,
)
from tyrex_pm.core.ids import CorrelationId, new_correlation_id, new_event_id
from tyrex_pm.core.ingress import FeedRole, build_ingress_timing, fingerprint_payload
from tyrex_pm.core.snapshots import ReferencePriceSnapshot, SettlementReferenceSnapshot

RTDS_URL = "wss://ws-live-data.polymarket.com"
TOPIC_CHAINLINK = "crypto_prices_chainlink"
TOPIC_BINANCE = "crypto_prices"
SYMBOL_CHAINLINK_BTC = "btc/usd"
SYMBOL_BINANCE_BTC = "btcusdt"


def chainlink_subscribe_message(*, filters: str = "") -> dict[str, Any]:
    """Official RTDS Chainlink subscription (empty filters returns all symbols)."""
    return {
        "action": "subscribe",
        "subscriptions": [
            {
                "topic": TOPIC_CHAINLINK,
                "type": "*",
                "filters": filters,
            }
        ],
    }


def binance_subscribe_message(*, mode: str = "filtered") -> dict[str, Any]:
    """RTDS Binance subscription.

    ``filtered`` uses documented ``filters: \"btcusdt\"`` (N1: may yield 0 msgs).
    ``unfiltered`` omits filters; client must local-filter ``btcusdt``.
    """
    sub: dict[str, Any] = {"topic": TOPIC_BINANCE, "type": "update"}
    if mode == "filtered":
        sub["filters"] = SYMBOL_BINANCE_BTC
    elif mode != "unfiltered":
        raise ValueError(f"unknown RTDS Binance mode: {mode!r}")
    return {"action": "subscribe", "subscriptions": [sub]}


def _ms_to_utc(value: Any) -> datetime:
    if value is None:
        raise ValueError("missing timestamp")
    ms = int(value)
    if ms < 10_000_000_000:
        ms *= 1000
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def parse_rtds_price_payload(msg: Mapping[str, Any]) -> tuple[str, str, Decimal, datetime, Any] | None:
    """Return (topic, symbol, value, source_ts, provider_seq) or None if not a price tick."""
    topic = str(msg.get("topic") or "")
    if topic not in (TOPIC_CHAINLINK, TOPIC_BINANCE):
        return None
    payload = msg.get("payload") or {}
    if not isinstance(payload, Mapping):
        return None
    # Historical snapshot envelopes use payload.data list — skip for tick path
    if "data" in payload and "value" not in payload:
        return None
    symbol = str(payload.get("symbol") or "").strip().lower()
    if not symbol:
        return None
    value = payload.get("value")
    if value is None:
        return None
    ts = payload.get("timestamp")
    if ts is None:
        ts = msg.get("timestamp")
    source_ts = _ms_to_utc(ts)
    provider_seq = msg.get("timestamp")
    return topic, symbol, Decimal(str(value)), source_ts, provider_seq


def normalize_chainlink_tick(
    msg: Mapping[str, Any],
    *,
    ts_received: datetime,
    receive_monotonic_ns: int,
    ingress_sequence: int,
    connection_generation: int,
    clock_uncertainty_ms: int | None,
    correlation_id: CorrelationId | None = None,
    expected_symbol: str = SYMBOL_CHAINLINK_BTC,
    last_source_ts_ms: int | None = None,
    time_view: Any | None = None,
    clock_snapshot_id: str | None = None,
) -> SettlementReferenceUpdated | None:
    parsed = parse_rtds_price_payload(msg)
    if parsed is None:
        return None
    topic, symbol, value, source_ts, provider_seq = parsed
    if topic != TOPIC_CHAINLINK:
        return None
    if symbol != expected_symbol.lower():
        return None
    source_ms = int(source_ts.timestamp() * 1000)
    ooo = None
    if last_source_ts_ms is not None and source_ms < last_source_ts_ms:
        ooo = "out_of_order"
    meta = build_ingress_timing(
        receive_wall_raw_utc=ts_received,
        receive_monotonic_ns=receive_monotonic_ns,
        ingress_sequence=ingress_sequence,
        connection_generation=connection_generation,
        time_view=time_view,
        provider_sequence_id=None if provider_seq is None else str(provider_seq),
        raw_fingerprint=fingerprint_payload(msg),
        late_or_out_of_order=ooo,
        role=FeedRole.SETTLEMENT_REFERENCE,
        subscription_mode="chainlink_empty_filters",
        clock_snapshot_id=clock_snapshot_id
        or (getattr(time_view, "clock_snapshot_id", None) if time_view else None),
    )
    if time_view is None and clock_uncertainty_ms is not None:
        from tyrex_pm.core.ingress import IngressMeta

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
    snap = SettlementReferenceSnapshot(
        symbol=symbol,
        price=value,
        ts_event=source_ts,
        venue="polymarket_rtds_chainlink",
        provider="chainlink",
    )
    return SettlementReferenceUpdated(
        event_id=new_event_id(),
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=None,
        ts_event=source_ts,
        ts_received=ts_received,
        source=EventSource.POLYMARKET_RTDS_CHAINLINK,
        settlement=snap,
        ingress=meta,
    )


def normalize_rtds_binance_tick(
    msg: Mapping[str, Any],
    *,
    ts_received: datetime,
    receive_monotonic_ns: int,
    ingress_sequence: int,
    connection_generation: int,
    clock_uncertainty_ms: int | None,
    subscription_mode: str,
    correlation_id: CorrelationId | None = None,
    expected_symbol: str = SYMBOL_BINANCE_BTC,
    last_source_ts_ms: int | None = None,
    time_view: Any | None = None,
    clock_snapshot_id: str | None = None,
) -> ReferencePriceUpdated | None:
    """Comparison/fallback only — never settlement truth."""
    parsed = parse_rtds_price_payload(msg)
    if parsed is None:
        return None
    topic, symbol, value, source_ts, provider_seq = parsed
    if topic != TOPIC_BINANCE:
        return None
    if symbol != expected_symbol.lower():
        return None
    source_ms = int(source_ts.timestamp() * 1000)
    ooo = None
    if last_source_ts_ms is not None and source_ms < last_source_ts_ms:
        ooo = "out_of_order"
    meta = build_ingress_timing(
        receive_wall_raw_utc=ts_received,
        receive_monotonic_ns=receive_monotonic_ns,
        ingress_sequence=ingress_sequence,
        connection_generation=connection_generation,
        time_view=time_view,
        provider_sequence_id=None if provider_seq is None else str(provider_seq),
        raw_fingerprint=fingerprint_payload(msg),
        late_or_out_of_order=ooo,
        role=FeedRole.COMPARISON_REFERENCE,
        subscription_mode=subscription_mode,
        clock_snapshot_id=clock_snapshot_id
        or (getattr(time_view, "clock_snapshot_id", None) if time_view else None),
    )
    if time_view is None and clock_uncertainty_ms is not None:
        from tyrex_pm.core.ingress import IngressMeta

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
    snap = ReferencePriceSnapshot(
        symbol=symbol.upper(),
        price=value,
        ts_event=source_ts,
        venue="polymarket_rtds_binance",
    )
    return ReferencePriceUpdated(
        event_id=new_event_id(),
        correlation_id=correlation_id or new_correlation_id(),
        causation_id=None,
        ts_event=source_ts,
        ts_received=ts_received,
        source=EventSource.POLYMARKET_RTDS_BINANCE,
        reference=snap,
        ingress=meta,
    )
