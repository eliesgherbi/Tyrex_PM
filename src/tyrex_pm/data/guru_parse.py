"""Parse Polymarket Data API trade rows into `GuruTradeSignal`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.core.types import GuruTradeSignal


def stable_source_trade_id(row: Mapping[str, Any]) -> str:
    """
    Stable id for Data API trades (no unique id in OpenAPI).

    Prefers `transactionHash` when present; falls back to a composite key.
    """

    tx = row.get("transactionHash") or ""
    ts = row.get("timestamp")
    asset = row.get("asset") or ""
    side = row.get("side") or ""
    size = row.get("size")
    price = row.get("price")
    if tx:
        return f"{tx}:{ts}:{asset}:{side}:{size}:{price}"
    return f"{ts}:{asset}:{side}:{size}:{price}"


def trade_row_to_signal(row: Mapping[str, Any]) -> GuruTradeSignal:
    tid = stable_source_trade_id(row)
    ts_raw = row.get("timestamp")
    try:
        ts_event_ms = int(ts_raw) if ts_raw is not None else 0
    except (TypeError, ValueError):
        ts_event_ms = 0

    side = str(row.get("side") or "")
    token = row.get("asset")
    token_id = str(token) if token is not None else None

    size_raw = _maybe_float(row.get("size"))
    price_raw = _maybe_float(row.get("price"))

    slug = row.get("slug") or row.get("eventSlug")
    raw_payload_ref = str(slug) if slug else None

    return GuruTradeSignal(
        source_trade_id=tid,
        ts_event_ms=ts_event_ms,
        side=side,
        token_id=token_id,
        size_raw=size_raw,
        price_raw=price_raw,
        raw_payload_ref=raw_payload_ref,
    )


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
