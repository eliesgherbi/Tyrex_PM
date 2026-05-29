"""P4.1: resolve live/resting exit orders into allocation ledger mutations."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, VenueOrderId
from tyrex_pm.runtime.allocation_runtime import _emit_allocation_fact
from tyrex_pm.runtime.coordinator import RuntimeCoordinator
from tyrex_pm.state.allocation_ledger import AllocationLedger


def _dec(raw: Any) -> Decimal:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _ledger(coord: RuntimeCoordinator) -> AllocationLedger | None:
    ledger = coord.allocation_ledger
    return ledger if isinstance(ledger, AllocationLedger) else None


def _run_id(coord: RuntimeCoordinator) -> str | None:
    return coord.allocation_ledger_run_id


def link_exit_reservation_venue_order(
    coord: RuntimeCoordinator,
    reservation_id: str,
    venue_order_id: str,
) -> None:
    ledger = _ledger(coord)
    if ledger is None:
        return
    ledger.set_reservation_venue_order_id(reservation_id, venue_order_id)


def _reservation_id_from_order_store(
    coord: RuntimeCoordinator,
    venue_order_id: str,
) -> str | None:
    ledger = _ledger(coord)
    if ledger is None:
        return None
    rid = ledger.find_reservation_id_by_venue_order_id(venue_order_id)
    if rid is not None:
        return rid
    vid = VenueOrderId(str(venue_order_id))
    for cid, lo in coord.orders.orders.items():
        if lo.side != Side.SELL or lo.venue_order_id != vid:
            continue
        if cid in ledger._reservations:  # noqa: SLF001 — lifecycle links via client_order_id
            return str(cid)
    return None


def apply_exit_fill_for_reservation(
    coord: RuntimeCoordinator,
    *,
    reservation_id: str,
    fill_qty: Decimal,
    source: str,
    dedup_key: str | None = None,
    venue_order_id: str | None = None,
) -> bool:
    ledger = _ledger(coord)
    run_id = _run_id(coord)
    if ledger is None or run_id is None:
        return False
    mut = ledger.apply_exit_fill(
        reservation_id,
        fill_qty,
        source=source,
        dedup_key=dedup_key,
        venue_order_id=venue_order_id,
    )
    if mut is None:
        return False
    _emit_allocation_fact(coord, run_id, mut, correlation_id=mut.correlation_id)
    return True


def release_exit_reservation(
    coord: RuntimeCoordinator,
    *,
    reservation_id: str | None = None,
    venue_order_id: str | None = None,
    source: str,
    reason: str,
) -> bool:
    ledger = _ledger(coord)
    run_id = _run_id(coord)
    if ledger is None or run_id is None:
        return False
    rid = reservation_id
    if rid is None and venue_order_id is not None:
        rid = _reservation_id_from_order_store(coord, venue_order_id)
    if rid is None:
        return False
    mut = ledger.release_reservation(rid, reason=reason, source=source)
    if mut is None:
        return False
    _emit_allocation_fact(coord, run_id, mut)
    return True


def resolve_exit_order_matched_qty(
    coord: RuntimeCoordinator,
    *,
    venue_order_id: str,
    size_matched: Decimal,
    source: str,
) -> bool:
    ledger = _ledger(coord)
    if ledger is None:
        return False
    rid = _reservation_id_from_order_store(coord, venue_order_id)
    if rid is None:
        return False
    row = ledger._reservations.get(rid)  # noqa: SLF001
    if row is None:
        return False
    delta = size_matched - row.applied_fill_qty
    if delta <= 0:
        return False
    dedup = f"{source}:matched:{venue_order_id}:{size_matched}"
    return apply_exit_fill_for_reservation(
        coord,
        reservation_id=rid,
        fill_qty=delta,
        source=source,
        dedup_key=dedup,
        venue_order_id=venue_order_id,
    )


def process_allocation_exit_from_order_store(coord: RuntimeCoordinator) -> None:
    """Promote matched qty from local OrderStore rows linked to reservations."""
    ledger = _ledger(coord)
    if ledger is None:
        return
    for rid in list(ledger._reservations.keys()):  # noqa: SLF001
        lo = coord.orders.orders.get(ClientOrderId(rid))
        if lo is None or lo.side != Side.SELL or lo.size_matched is None:
            continue
        vid = str(lo.venue_order_id) if lo.venue_order_id is not None else None
        if vid is None:
            continue
        resolve_exit_order_matched_qty(
            coord,
            venue_order_id=vid,
            size_matched=lo.size_matched,
            source="reconcile",
        )


def process_user_ws_allocation_exit(coord: RuntimeCoordinator, msg: dict[str, Any]) -> None:
    """Apply allocation lifecycle updates from one user-channel payload."""
    t = str(msg.get("type", "")).upper()
    if t == "CANCELLATION":
        oid = msg.get("id")
        if oid:
            release_exit_reservation(
                coord,
                venue_order_id=str(oid),
                source="user_ws",
                reason="cancelled",
            )
        return
    if t == "UPDATE":
        oid = msg.get("id")
        if not oid:
            return
        if msg.get("size_matched") is not None:
            resolve_exit_order_matched_qty(
                coord,
                venue_order_id=str(oid),
                size_matched=_dec(msg.get("size_matched")),
                source="user_ws",
            )
        return
    if t != "TRADE":
        return
    side_raw = str(msg.get("side", "")).upper()
    if side_raw != "SELL":
        return
    status = str(msg.get("status", "")).upper()
    if status != "CONFIRMED":
        return
    sz = _dec(msg.get("size") or 0)
    if sz <= 0:
        return
    trade_id = msg.get("id") or msg.get("trade_id")
    order_id = (
        msg.get("maker_order_id")
        or msg.get("taker_order_id")
        or msg.get("order_id")
        or msg.get("orderID")
    )
    if order_id is None:
        return
    rid = _reservation_id_from_order_store(coord, str(order_id))
    if rid is None:
        return
    dedup = f"user_ws_trade:{trade_id}" if trade_id is not None else f"user_ws_trade:{order_id}:{sz}:{status}"
    apply_exit_fill_for_reservation(
        coord,
        reservation_id=rid,
        fill_qty=sz,
        source="user_ws_fill",
        dedup_key=str(dedup),
        venue_order_id=str(order_id),
    )
