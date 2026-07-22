"""N6 account classification for authenticated read-only reconciliation."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class AccountClassification(str, Enum):
    KNOWN_FLAT_SELECTED_MARKET = "KNOWN_FLAT_SELECTED_MARKET"
    KNOWN_ACKNOWLEDGED_EXTERNAL_POSITIONS = "KNOWN_ACKNOWLEDGED_EXTERNAL_POSITIONS"
    OPEN_ORDERS_PRESENT = "OPEN_ORDERS_PRESENT"
    BALANCE_DISAGREEMENT = "BALANCE_DISAGREEMENT"
    INVENTORY_DISAGREEMENT = "INVENTORY_DISAGREEMENT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, kw_only=True)
class AcknowledgedExternalPosition:
    """Historical position that must remain visible and untouched."""

    label: str
    token_id: str | None = None
    condition_id: str | None = None
    status: str = "RESOLVED_REDEEMABLE"
    notes: str = ""


@dataclass
class AccountClassifyResult:
    classifications: list[AccountClassification] = field(default_factory=list)
    selected_market_flat: bool = False
    globally_flat: bool = False
    open_order_count: int = 0
    acknowledged_external: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classifications": [c.value for c in self.classifications],
            "selected_market_flat": self.selected_market_flat,
            "globally_flat": self.globally_flat,
            "open_order_count": self.open_order_count,
            "acknowledged_external": list(self.acknowledged_external),
            "evidence": dict(self.evidence),
            "ready_for_n7_mutations": False,
            "mutations_enabled": False,
        }


def classify_account(
    *,
    open_orders: Sequence[Mapping[str, Any]],
    positions: Sequence[Mapping[str, Any]],
    selected_market_token_ids: set[str],
    acknowledged: Sequence[AcknowledgedExternalPosition] = (),
    balance_disagreement: bool = False,
    inventory_disagreement: bool = False,
    unknown: bool = False,
) -> AccountClassifyResult:
    """Classify account for N6 read-only reconciliation.

    Acknowledged historical positions are always listed and prevent
    ``globally_flat=True``. They may be excluded from selected-market flatness
    only via explicit acknowledgment — never by hiding them.
    """
    result = AccountClassifyResult()
    result.open_order_count = len(list(open_orders))
    ack_ids = {
        a.token_id for a in acknowledged if a.token_id
    } | {a.condition_id for a in acknowledged if a.condition_id}
    result.acknowledged_external = [
        {
            "label": a.label,
            "token_id": a.token_id,
            "condition_id": a.condition_id,
            "status": a.status,
            "notes": a.notes,
            "untouched": True,
            "not_strategy_inventory": True,
        }
        for a in acknowledged
    ]

    if unknown:
        result.classifications.append(AccountClassification.UNKNOWN)
    if balance_disagreement:
        result.classifications.append(AccountClassification.BALANCE_DISAGREEMENT)
    if inventory_disagreement:
        result.classifications.append(AccountClassification.INVENTORY_DISAGREEMENT)
    if result.open_order_count > 0:
        result.classifications.append(AccountClassification.OPEN_ORDERS_PRESENT)

    selected_qty = 0.0
    other_qty = 0.0
    for pos in positions:
        tid = str(pos.get("token_id") or pos.get("instrument_token_id") or "")
        size = float(pos.get("size") or pos.get("quantity") or 0)
        if tid in selected_market_token_ids:
            selected_qty += abs(size)
        else:
            other_qty += abs(size)

    result.selected_market_flat = selected_qty == 0 and result.open_order_count == 0
    if result.selected_market_flat:
        result.classifications.append(AccountClassification.KNOWN_FLAT_SELECTED_MARKET)

    if result.acknowledged_external or other_qty > 0:
        result.classifications.append(
            AccountClassification.KNOWN_ACKNOWLEDGED_EXTERNAL_POSITIONS
        )
        result.globally_flat = False
    else:
        result.globally_flat = result.selected_market_flat and not result.classifications

    if result.acknowledged_external and result.globally_flat:
        result.globally_flat = False

    result.evidence = {
        "selected_abs_qty": selected_qty,
        "other_abs_qty": other_qty,
        "ack_keys": sorted(str(x) for x in ack_ids if x),
    }
    # Dedupe classifications preserving order
    seen: set[str] = set()
    uniq: list[AccountClassification] = []
    for c in result.classifications:
        if c.value not in seen:
            seen.add(c.value)
            uniq.append(c)
    result.classifications = uniq
    return result
