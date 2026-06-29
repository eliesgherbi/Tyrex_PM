"""Entry order fill lifecycle — separates submit/resting from owned inventory.

OrderStore tracks submitted/resting orders. AllocationLedger credits only fill evidence.
Strategies must use this module (not ledger alone) to decide owned/sellable qty.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId
from tyrex_pm.runtime.exit_lifecycle import MATCHED_STATUSES, oms_status_is_matched, parse_taking_amount
from tyrex_pm.state import fill_state


class EntryFillStatus(str, Enum):
    SUBMITTED = "SUBMITTED"
    RESTING = "RESTING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    MATCHED = "MATCHED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class OrderFillSnapshot:
    token_id: TokenId
    owner_id: str | None
    client_order_id: str | None
    submitted_qty: Decimal
    filled_qty: Decimal
    confirmed_qty: Decimal
    remaining_qty: Decimal
    status: EntryFillStatus
    source: str
    sellable_qty: Decimal

    @property
    def owns_inventory(self) -> bool:
        return self.sellable_qty > 0


def classify_ack_status(raw: str | None) -> EntryFillStatus:
    st = str(raw or "").strip().lower()
    if st in MATCHED_STATUSES or st == "filled":
        return EntryFillStatus.MATCHED
    if st in {"live", "resting", "open", "delayed"}:
        return EntryFillStatus.RESTING
    if st in {"cancelled", "canceled"}:
        return EntryFillStatus.CANCELLED
    if st in {"expired"}:
        return EntryFillStatus.EXPIRED
    if st in {"failed", "rejected"}:
        return EntryFillStatus.FAILED
    if st:
        return EntryFillStatus.SUBMITTED
    return EntryFillStatus.SUBMITTED


def filled_qty_from_match_evidence(
    match_evidence: dict[str, Any],
    approved_size: Decimal,
    *,
    apply_local_shadow_fill: bool = False,
) -> Decimal:
    """Return filled share qty for allocation credit; 0 if order is not matched/filled."""
    if apply_local_shadow_fill:
        return approved_size
    if not oms_status_is_matched(match_evidence):
        return Decimal("0")
    taking = parse_taking_amount(match_evidence)
    if taking is not None and taking > 0:
        return min(approved_size, taking)
    return approved_size


def is_resting_ack(match_evidence: dict[str, Any]) -> bool:
    st = str(match_evidence.get("match_status", "")).lower()
    return st in {"live", "resting", "open", "delayed"} and not oms_status_is_matched(match_evidence)


def _trade_buy_qty(coord, token_id: TokenId) -> tuple[Decimal, Decimal, str]:
    """Return (confirmed_qty, matched_qty, source) from user-WS trade ledger."""
    wallet = coord.wallet
    confirmed = Decimal("0")
    matched = Decimal("0")
    source = "none"
    for rec in reversed(wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != Side.BUY:
            continue
        status = str(rec.status).upper()
        if fill_state.is_allocation_final(status):
            confirmed = max(confirmed, rec.size)
            source = rec.source or "user_ws"
            break
    if confirmed > 0:
        return confirmed, matched, source
    for rec in reversed(wallet.trade_fill_records):
        if rec.token_id != token_id or rec.side != Side.BUY:
            continue
        status = str(rec.status).upper()
        if fill_state.is_execution_evidence(status):
            matched = max(matched, rec.size)
            source = rec.source or "user_ws"
            break
    return confirmed, matched, source


def _local_order_fill_qty(coord, client_order_id: str | None) -> Decimal:
    if not client_order_id:
        return Decimal("0")
    lo = coord.orders.orders.get(ClientOrderId(client_order_id))
    if lo is None or lo.side != Side.BUY:
        return Decimal("0")
    if lo.size_matched is not None and lo.size_matched > 0:
        return lo.size_matched
    if lo.ack_status and str(lo.ack_status).lower() in MATCHED_STATUSES:
        if lo.original_size is not None:
            if lo.remaining < lo.original_size:
                return lo.original_size - lo.remaining
            return lo.original_size
    return Decimal("0")


def resolve_leg_fill_snapshot(
    coord,
    *,
    token_id: TokenId,
    owner_id: str | None = None,
    client_order_id: str | None = None,
    submitted_qty: Decimal | None = None,
    include_venue_position: bool = True,
) -> OrderFillSnapshot:
    """Resolve owned/sellable qty from fill/position evidence (not ledger)."""
    confirmed, matched_ws, ws_source = _trade_buy_qty(coord, token_id)
    oms_fill = _local_order_fill_qty(coord, client_order_id)
    venue_qty = Decimal("0")
    pos = coord.wallet.positions.get(token_id)
    if pos is not None and pos.qty > 0:
        venue_qty = pos.qty

    filled = Decimal("0")
    source = "none"
    status = EntryFillStatus.SUBMITTED

    if confirmed > 0:
        filled = confirmed
        source = ws_source
        status = EntryFillStatus.CONFIRMED
    elif matched_ws > 0:
        filled = matched_ws
        source = ws_source
        status = EntryFillStatus.MATCHED
    elif oms_fill > 0:
        filled = oms_fill
        source = "oms_matched"
        status = EntryFillStatus.MATCHED
    elif include_venue_position and venue_qty > 0:
        filled = venue_qty
        source = "venue_position"
        status = EntryFillStatus.MATCHED

    if client_order_id:
        lo = coord.orders.orders.get(ClientOrderId(client_order_id))
        if lo is not None:
            if lo.original_size is not None:
                submitted_qty = submitted_qty or lo.original_size
            if filled <= 0:
                status = classify_ack_status(lo.ack_status)
            elif lo.original_size is not None and filled < lo.original_size and lo.remaining > 0:
                status = EntryFillStatus.PARTIALLY_FILLED

    sellable = min(filled, venue_qty) if venue_qty > 0 else filled
    if sellable <= 0 and filled > 0 and venue_qty <= 0:
        sellable = filled

    sub = submitted_qty or filled or Decimal("0")
    remaining = sub - filled if sub > filled else Decimal("0")

    return OrderFillSnapshot(
        token_id=token_id,
        owner_id=owner_id,
        client_order_id=client_order_id,
        submitted_qty=sub,
        filled_qty=filled,
        confirmed_qty=confirmed,
        remaining_qty=max(Decimal("0"), remaining),
        status=status,
        source=source,
        sellable_qty=sellable,
    )
