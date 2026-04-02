"""Parse Polymarket Data API trade rows into `GuruTradeSignal`."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from tyrex_pm.core.types import GuruTradeSignal


def api_timestamp_to_ms(raw: Any) -> int:
    """
    Normalize Data API ``timestamp`` (seconds or ms) to integer milliseconds.
    Heuristic: values greater than ``1e12`` are treated as ms.
    """

    if raw is None:
        return 0
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 0
    if v > 10**12:
        return v
    return v * 1000


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
    ts_event_ms = api_timestamp_to_ms(row.get("timestamp"))

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


def activity_trade_row_to_signal(row: Mapping[str, Any]) -> GuruTradeSignal:
    """
    Map ``GET /activity`` row with ``type=TRADE`` to ``GuruTradeSignal``.

    Shape matches Trade schema closely; reuse ``trade_row_to_signal`` after
    normalizing timestamp to ms for stable id hashing.
    """

    d = dict(row)
    d["timestamp"] = api_timestamp_to_ms(row.get("timestamp"))
    return trade_row_to_signal(d)


def _maybe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
