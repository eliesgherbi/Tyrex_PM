"""Cross-cutting value types (v1.04 guru signal schema)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GuruTradeSignal:
    """
    Normalized guru trade from Polymarket Data API (`GET /trades`).

    `token_id` maps to the API `asset` field (conditional token id). `InstrumentId`
    resolution stays in loaders / strategy (v1.05+), not in the data poller.
    """

    source_trade_id: str
    ts_event_ms: int
    side: str
    token_id: str | None
    size_raw: float | None
    price_raw: float | None
    raw_payload_ref: str | None


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """
    Copy intent after signal + sizing (before risk + venue translation).

    `correlation_id` is the guru `source_trade_id` for traceability.
    """

    correlation_id: str
    token_id: str
    side: str
    quantity: float
    signal_kind: str  # "entry" | "exit"
    reason_code: str
    price_ref: float | None = None
