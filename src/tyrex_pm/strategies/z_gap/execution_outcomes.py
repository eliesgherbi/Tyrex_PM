"""Z-Gap entry/exit execution outcome taxonomy (D3.b)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

OUTCOME_BLOCKED = "blocked"
OUTCOME_REJECTED = "rejected"
OUTCOME_ZERO_FILL = "zero_fill"
OUTCOME_PARTIAL_FILL = "partial_fill"
OUTCOME_FULL_FILL = "full_fill"
OUTCOME_UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class EntryExecutionOutcome:
    category: str
    client_order_id: str | None = None
    venue_order_id: str | None = None
    submission_attempted: bool = False
    submission_acknowledged: bool = False
    reject_stage: str | None = None
    reject_code: str | None = None
    requested_quantity: Decimal = Decimal("0")
    confirmed_filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    order_status: str | None = None
    terminal_interpretation: str = ""

    def to_fact_payload(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "client_order_id": self.client_order_id,
            "venue_order_id": self.venue_order_id,
            "submission_attempted": self.submission_attempted,
            "submission_acknowledged": self.submission_acknowledged,
            "reject_stage": self.reject_stage,
            "reject_code": self.reject_code,
            "requested_quantity": str(self.requested_quantity),
            "confirmed_filled_quantity": str(self.confirmed_filled_quantity),
            "average_fill_price": str(self.average_fill_price) if self.average_fill_price is not None else None,
            "order_status": self.order_status,
            "terminal_interpretation": self.terminal_interpretation,
        }


def classify_entry_outcome(
    *,
    blocked: bool = False,
    blocked_reason: str | None = None,
    rejected: bool = False,
    reject_stage: str | None = None,
    reject_code: str | None = None,
    submitted: bool = False,
    acknowledged: bool = False,
    requested_qty: Decimal = Decimal("0"),
    filled_qty: Decimal = Decimal("0"),
    avg_price: Decimal | None = None,
    order_status: str | None = None,
    client_order_id: str | None = None,
    venue_order_id: str | None = None,
    unresolved: bool = False,
) -> EntryExecutionOutcome:
    if blocked:
        return EntryExecutionOutcome(
            category=OUTCOME_BLOCKED,
            reject_stage="pre_submit",
            reject_code=blocked_reason,
            requested_quantity=requested_qty,
            terminal_interpretation="entry_blocked_before_submission",
            client_order_id=client_order_id,
        )
    if rejected:
        return EntryExecutionOutcome(
            category=OUTCOME_REJECTED,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            submission_attempted=submitted,
            submission_acknowledged=acknowledged,
            reject_stage=reject_stage,
            reject_code=reject_code,
            requested_quantity=requested_qty,
            confirmed_filled_quantity=filled_qty,
            order_status=order_status,
            terminal_interpretation="entry_rejected_after_submission_attempt",
        )
    if unresolved:
        return EntryExecutionOutcome(
            category=OUTCOME_UNRESOLVED,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            submission_attempted=submitted,
            submission_acknowledged=acknowledged,
            requested_quantity=requested_qty,
            confirmed_filled_quantity=filled_qty,
            order_status=order_status,
            terminal_interpretation="entry_execution_unresolved",
        )
    if submitted and filled_qty <= 0:
        return EntryExecutionOutcome(
            category=OUTCOME_ZERO_FILL,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            submission_attempted=True,
            submission_acknowledged=acknowledged,
            requested_quantity=requested_qty,
            confirmed_filled_quantity=Decimal("0"),
            order_status=order_status,
            terminal_interpretation="fak_submitted_zero_fill",
        )
    if filled_qty > 0 and requested_qty > 0 and filled_qty < requested_qty:
        return EntryExecutionOutcome(
            category=OUTCOME_PARTIAL_FILL,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            submission_attempted=True,
            submission_acknowledged=acknowledged,
            requested_quantity=requested_qty,
            confirmed_filled_quantity=filled_qty,
            average_fill_price=avg_price,
            order_status=order_status,
            terminal_interpretation="partial_buy_fill_confirmed",
        )
    if filled_qty > 0:
        return EntryExecutionOutcome(
            category=OUTCOME_FULL_FILL,
            client_order_id=client_order_id,
            venue_order_id=venue_order_id,
            submission_attempted=True,
            submission_acknowledged=acknowledged,
            requested_quantity=requested_qty,
            confirmed_filled_quantity=filled_qty,
            average_fill_price=avg_price,
            order_status=order_status,
            terminal_interpretation="full_buy_fill_confirmed",
        )
    return EntryExecutionOutcome(
        category=OUTCOME_UNRESOLVED,
        terminal_interpretation="entry_outcome_unknown",
    )
