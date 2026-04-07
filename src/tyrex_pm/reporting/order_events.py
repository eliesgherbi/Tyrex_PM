"""Map Nautilus OrderEvent → reporting facts (INT-ORD-01)."""

from __future__ import annotations

import hashlib
from typing import Any, Callable

from nautilus_trader.model.events.order import (
    OrderAccepted,
    OrderCanceled,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderRejected,
    OrderSubmitted,
)

from tyrex_pm.reporting.capital_observability import venue_denial_insufficient_balance_likely

EmitFn = Callable[[str, dict[str, Any]], None]


def _gc_attr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        val = getattr(obj, name, default)
        return default if val is None else val
    except Exception:  # noqa: BLE001
        return default


def _coid_str(event: OrderEvent) -> str:
    c = _gc_attr(event, "client_order_id")
    return str(c) if c is not None else ""


def _void_str(event: OrderEvent) -> str | None:
    v = _gc_attr(event, "venue_order_id")
    return str(v) if v is not None else None


def _inst_str(event: OrderEvent) -> str | None:
    i = _gc_attr(event, "instrument_id")
    return str(i) if i is not None else None


def _status_name(event: OrderEvent) -> str:
    st = _gc_attr(event, "order_status") or _gc_attr(event, "status")
    if st is None:
        return type(event).__name__
    name = getattr(st, "name", None)
    return str(name) if name else str(st)


def _ts_ns(event: OrderEvent) -> int | None:
    ts = _gc_attr(event, "ts_event")
    if ts is None:
        return None
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def emit_order_event_facts(
    event: OrderEvent,
    *,
    run_id: str,
    correlation_lookup: Callable[[str], str | None],
    emit: EmitFn,
) -> None:
    """
    ``correlation_lookup`` maps client_order_id str -> correlation_id or None.
    """
    coid = _coid_str(event)
    if not coid:
        return
    correlation_id = correlation_lookup(coid)
    vid = _void_str(event)
    inst = _inst_str(event)
    ts_ns = _ts_ns(event)

    def _lifecycle(status: str, extra: dict[str, Any] | None = None) -> None:
        pl: dict[str, Any] = {
            "client_order_id": coid,
            "status": status,
        }
        if vid is not None:
            pl["venue_order_id"] = vid
        if correlation_id is not None:
            pl["correlation_id"] = correlation_id
        if inst is not None:
            pl["instrument_id"] = inst
        if ts_ns is not None:
            pl["ts_event_ns"] = ts_ns
        if extra:
            pl.update(extra)
        emit("order_lifecycle", pl)

    if isinstance(event, OrderSubmitted):
        _lifecycle("SUBMITTED")
    elif isinstance(event, OrderAccepted):
        _lifecycle("ACCEPTED")
    elif isinstance(event, OrderRejected):
        _lifecycle(
            "REJECTED",
            {"reason": str(_gc_attr(event, "reason", ""))},
        )
    elif isinstance(event, OrderDenied):
        reason_s = str(_gc_attr(event, "reason", ""))
        _lifecycle(
            "DENIED",
            {
                "reason": reason_s,
                "venue_insufficient_balance_likely": venue_denial_insufficient_balance_likely(
                    reason_s,
                ),
            },
        )
    elif isinstance(event, OrderCanceled):
        _lifecycle("CANCELED")
    elif isinstance(event, OrderFilled):
        last_qty = _gc_attr(event, "last_qty")
        last_px = _gc_attr(event, "last_px")
        qty_s = str(last_qty) if last_qty is not None else ""
        px_s = str(last_px) if last_px is not None else ""
        h = hashlib.sha256(
            f"{coid}|{vid or ''}|{ts_ns}|{qty_s}|{px_s}".encode("utf-8"),
        ).hexdigest()[:32]
        fill_pl: dict[str, Any] = {
            "client_order_id": coid,
            "fill_event_id": f"fill_{h}",
            "last_qty": qty_s,
            "last_px": px_s,
        }
        if vid is not None:
            fill_pl["venue_order_id"] = vid
        if correlation_id is not None:
            fill_pl["correlation_id"] = correlation_id
        if inst is not None:
            fill_pl["instrument_id"] = inst
        if ts_ns is not None:
            fill_pl["ts_event_ns"] = ts_ns
        liq = _gc_attr(event, "liquidity_side")
        if liq is not None:
            fill_pl["liquidity_side"] = str(
                getattr(liq, "name", liq),
            )
        emit("fill", fill_pl)
        _lifecycle(_status_name(event), {"last_qty": qty_s})
    else:
        _lifecycle(_status_name(event))
