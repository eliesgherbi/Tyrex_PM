"""Persisted protected lot model for virtual TP/SL (v1, long-only)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProtectedLot:
    schema_version: int = 1
    lot_id: str = ""
    instrument_id: str = ""
    token_id: str = ""
    entry_guru_correlation_id: str | None = None
    entry_client_order_id: str = ""
    #: Cumulative entry fill qty (Tier B).
    entry_qty_filled: float = 0.0
    entry_vwap: float = 0.0
    #: Remaining qty this lot protects (decreases on exit fills).
    qty_open: float = 0.0
    tp_pct: float = 0.0
    sl_pct: float = 0.0
    tp_trigger_price: float | None = None
    sl_trigger_price: float | None = None
    state: str = "PENDING_ENTRY"
    tp_armed: bool = True
    sl_armed: bool = True
    exit_client_order_id: str | None = None
    exit_kind: str | None = None
    last_trigger_ts_ms: int | None = None
    exit_attempts: int = 0
    #: Monotonic per lot for unique virtual exit ``correlation_id`` / COID.
    exit_nonce: int = 0
    last_exit_was_market: bool = False
    created_ts_ms: int = 0
    updated_ts_ms: int = 0
    #: Wall time when this lot first entered ``ARMED`` (ms); used for Tier A flat grace.
    armed_at_ts_ms: int | None = None
    #: Recovery: skip immediate resubmit if we reconciled an open exit from cache.
    recovery_seen_open_exit: bool = field(default=False, repr=False)
    #: One-shot automatic SL market→limit fallback (not persisted).
    sl_limit_fallback_used: bool = field(default=False, repr=False)

    def to_json_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("recovery_seen_open_exit", None)
        d.pop("sl_limit_fallback_used", None)
        return d

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> ProtectedLot:
        data = dict(data)
        data.pop("recovery_seen_open_exit", None)
        data.pop("sl_limit_fallback_used", None)
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            lot_id=str(data.get("lot_id", "")),
            instrument_id=str(data.get("instrument_id", "")),
            token_id=str(data.get("token_id", "")),
            entry_guru_correlation_id=data.get("entry_guru_correlation_id"),
            entry_client_order_id=str(data.get("entry_client_order_id", "")),
            entry_qty_filled=float(data.get("entry_qty_filled", 0.0)),
            entry_vwap=float(data.get("entry_vwap", 0.0)),
            qty_open=float(data.get("qty_open", data.get("entry_qty_filled", 0.0))),
            tp_pct=float(data.get("tp_pct", 0.0)),
            sl_pct=float(data.get("sl_pct", 0.0)),
            tp_trigger_price=(
                float(data["tp_trigger_price"]) if data.get("tp_trigger_price") is not None else None
            ),
            sl_trigger_price=(
                float(data["sl_trigger_price"]) if data.get("sl_trigger_price") is not None else None
            ),
            state=str(data.get("state", "PENDING_ENTRY")),
            tp_armed=bool(data.get("tp_armed", True)),
            sl_armed=bool(data.get("sl_armed", True)),
            exit_client_order_id=data.get("exit_client_order_id"),
            exit_kind=data.get("exit_kind"),
            last_trigger_ts_ms=(
                int(data["last_trigger_ts_ms"]) if data.get("last_trigger_ts_ms") is not None else None
            ),
            exit_attempts=int(data.get("exit_attempts", 0)),
            exit_nonce=int(data.get("exit_nonce", 0)),
            last_exit_was_market=bool(data.get("last_exit_was_market", False)),
            created_ts_ms=int(data.get("created_ts_ms", 0)),
            updated_ts_ms=int(data.get("updated_ts_ms", 0)),
            armed_at_ts_ms=(
                int(data["armed_at_ts_ms"])
                if data.get("armed_at_ts_ms") is not None
                else None
            ),
        )


LOT_TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "DISARMED_DRIFT",
        "DISARMED_EXTERNAL_FLAT",
        "FAILED",
    },
)
