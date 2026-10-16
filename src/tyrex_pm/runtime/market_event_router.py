"""Shared routing of normalized reference events into a MarketSessionRuntime.

The live market-data runtime and the offline research replay both call these
functions, so a reference tick reaches the PTB engine, the basis estimate and the
indicator graph through exactly one path. Indicator updates happen inside
``MarketSessionRuntime.ingest_binance``; callers must not also feed the graph.

Book events are not routed here: ``MarketStateStore`` consumes
``BookSnapshotReceived`` / ``BookDeltaReceived`` directly in both paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tyrex_pm.core.events import ReferencePriceUpdated, SettlementReferenceUpdated
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryTickView
from tyrex_pm.indicators.causal_pairing import PriceTickView, TradingReferenceIdentity

if TYPE_CHECKING:
    from tyrex_pm.runtime.market_runtime import MarketSessionRuntime


def route_settlement_reference(
    runtime: MarketSessionRuntime, event: SettlementReferenceUpdated
) -> tuple[str, ...]:
    """Ingest one settlement-reference tick into every open session.

    Returns the window ids that received the tick so the caller can apply its own
    sealing policy (live attestation differs from replay).
    """
    meta = event.ingress
    tick = BoundaryTickView(
        value=event.settlement.price,
        source_ts=event.ts_event,
        receive_wall_raw_utc=event.ts_received,
        receive_wall_corrected_utc=(None if meta is None else meta.receive_wall_corrected_utc),
        receive_monotonic_ns=0 if meta is None else meta.receive_monotonic_ns,
        ingress=meta,
        raw_fingerprint=None if meta is None else meta.raw_fingerprint,
    )
    routed: list[str] = []
    for sess in (runtime.active, runtime.prepared_next):
        if sess is None:
            continue
        runtime.ingest_chainlink(
            window_id=sess.window_id,
            market_id=sess.market_id,
            tick=tick,
        )
        routed.append(sess.window_id)
    return tuple(routed)


def route_trading_reference(
    runtime: MarketSessionRuntime, event: ReferencePriceUpdated
) -> bool:
    """Ingest one Binance spot trade. Returns False for other reference sources."""
    if event.source.value != "binance":
        return False
    meta = event.ingress
    runtime.ingest_binance(
        PriceTickView(
            value=event.reference.price,
            source_ts=event.ts_event,
            receive_wall_raw_utc=event.ts_received,
            receive_wall_corrected_utc=(None if meta is None else meta.receive_wall_corrected_utc),
            receive_monotonic_ns=0 if meta is None else meta.receive_monotonic_ns,
            identity=TradingReferenceIdentity.BINANCE_SPOT,
            ingress=meta,
        )
    )
    return True
