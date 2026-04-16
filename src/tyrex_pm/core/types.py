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
    Virtual exits use ``correlation_id`` like ``ve:{lot_id}:{kind}`` and set ``intent_origin``.
    """

    correlation_id: str
    token_id: str
    side: str
    quantity: float
    signal_kind: str  # "entry" | "exit"
    reason_code: str
    price_ref: float | None = None
    #: ``guru`` | ``virtual_tp`` | ``virtual_sl`` — concurrent guru-rest cap uses tags/COID only.
    intent_origin: str = "guru"
    virtual_lot_id: str | None = None
    #: When ``intent_origin`` is virtual: ``tp`` | ``sl``.
    virtual_exit_kind: str | None = None
