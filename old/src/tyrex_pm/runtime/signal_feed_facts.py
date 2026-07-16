"""Fact-ready payload builders for Z-Gap / generic signal feeds (A0.2)."""

from __future__ import annotations

from typing import Any

from tyrex_pm.state.signal_state_store import SignalSnapshot

FACT_TYPE_SIGNAL_FEED_HEALTH = "signal_feed_health"
FACT_TYPE_BASIS_COMPUTED = "basis_computed"
FACT_TYPE_PRICE_TO_BEAT_OBSERVED = "price_to_beat_observed"


def build_signal_feed_health_payload(
    *,
    feed: str,
    connected: bool,
    freshness: str,
    last_source_ts: str | None,
    last_recv_ts: str | None,
    age_ms: float | None,
    stale: bool,
    price: str | None = None,
) -> dict[str, Any]:
    return {
        "feed": feed,
        "connected": connected,
        "freshness": freshness,
        "last_source_ts": last_source_ts,
        "last_recv_ts": last_recv_ts,
        "age_ms": age_ms,
        "stale": stale,
        "price": price,
    }


def build_signal_feed_health_from_snapshot(snap: SignalSnapshot, *, feed: str) -> dict[str, Any]:
    if feed == "binance":
        return build_signal_feed_health_payload(
            feed=feed,
            connected=snap.binance_recv_ts is not None,
            freshness=snap.binance_freshness,
            last_source_ts=snap.binance_source_ts.isoformat() if snap.binance_source_ts else None,
            last_recv_ts=snap.binance_recv_ts.isoformat() if snap.binance_recv_ts else None,
            age_ms=snap.binance_age_ms,
            stale=snap.binance_freshness == "stale",
            price=str(snap.binance_price) if snap.binance_price is not None else None,
        )
    return build_signal_feed_health_payload(
        feed=feed,
        connected=snap.chainlink_recv_ts is not None,
        freshness=snap.chainlink_freshness,
        last_source_ts=snap.chainlink_source_ts.isoformat() if snap.chainlink_source_ts else None,
        last_recv_ts=snap.chainlink_recv_ts.isoformat() if snap.chainlink_recv_ts else None,
        age_ms=snap.chainlink_age_ms,
        stale=snap.chainlink_freshness == "stale",
        price=str(snap.chainlink_price) if snap.chainlink_price is not None else None,
    )


def build_basis_computed_payload(snap: SignalSnapshot) -> dict[str, Any]:
    return {
        "S": str(snap.binance_price) if snap.binance_price is not None else None,
        "S_CL": str(snap.chainlink_price) if snap.chainlink_price is not None else None,
        "basis_bps": str(snap.basis_bps) if snap.basis_bps is not None else None,
        "basis_status": snap.basis_status,
        "chainlink_fresh": snap.chainlink_fresh,
        "binance_freshness": snap.binance_freshness,
        "chainlink_freshness": snap.chainlink_freshness,
        "snapshot_ts": snap.snapshot_ts.isoformat(),
    }


def build_price_to_beat_observed_payload(
    *,
    market_id: str,
    price_to_beat: str | None,
    ptb_status: str,
    ptb_lag_ms: float | None,
    event_start_ts: float | None = None,
    event_end_ts: float | None = None,
    reference_source: str = "polymarket_rtds_chainlink",
) -> dict[str, Any]:
    return {
        "market_id": market_id,
        "price_to_beat": price_to_beat,
        "ptb_status": ptb_status,
        "ptb_lag_ms": ptb_lag_ms,
        "event_start_ts": event_start_ts,
        "event_end_ts": event_end_ts,
        "reference_source": reference_source,
    }
