"""Framework submission lineage (authoritative when venue idempotency is insufficient).

Polymarket CLOB does not expose a caller-controlled idempotent client order ID
on POST /order. Order identity is the EIP-712 signed order hash (salt/nonce);
identical re-posts may yield INVALID_ORDER_DUPLICATED, but lost-ack recovery
cannot rely on a venue-side idempotency key. Therefore:

    intent_id → plan_id → request_fingerprint → submission_attempt_id → venue_order_id?

is the authoritative dedupe / ambiguity key for N6.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.ids import OrderId
from tyrex_pm.core.intents import IntentId
from tyrex_pm.planning.plan import PlanId


class SubmissionAttemptState(str, Enum):
    CREATED = "CREATED"
    DISPATCHED = "DISPATCHED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    AMBIGUOUS = "AMBIGUOUS"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True, kw_only=True)
class SubmissionLineage:
    intent_id: IntentId
    plan_id: PlanId
    request_fingerprint: str
    submission_attempt_id: str
    local_order_id: OrderId | None = None
    venue_order_id: str | None = None
    state: SubmissionAttemptState = SubmissionAttemptState.CREATED
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id.value,
            "plan_id": self.plan_id.value,
            "request_fingerprint": self.request_fingerprint,
            "submission_attempt_id": self.submission_attempt_id,
            "local_order_id": None if self.local_order_id is None else self.local_order_id.value,
            "venue_order_id": self.venue_order_id,
            "state": self.state.value,
            "created_at": None if self.created_at is None else self.created_at.isoformat(),
        }


def new_submission_attempt_id() -> str:
    return str(uuid4())


def request_fingerprint(
    *,
    intent_id: IntentId | str,
    plan_id: PlanId | str,
    market_id: str,
    instrument_id: str,
    side: str,
    quantity: Decimal | str,
    limit_price: Decimal | str,
    client_order_id: str,
) -> str:
    """Stable fingerprint of the intended submission (not the venue ack)."""
    payload = {
        "intent_id": intent_id if isinstance(intent_id, str) else intent_id.value,
        "plan_id": plan_id if isinstance(plan_id, str) else plan_id.value,
        "market_id": market_id,
        "instrument_id": instrument_id,
        "side": side,
        "quantity": str(quantity),
        "limit_price": str(limit_price),
        "client_order_id": client_order_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class LineageRegistry:
    """Tracks attempts; blocks duplicate entry for the same fingerprint while AMBIGUOUS."""

    _by_attempt: dict[str, SubmissionLineage]
    _by_fingerprint: dict[str, list[str]]

    def __init__(self) -> None:
        self._by_attempt = {}
        self._by_fingerprint = {}

    def register(self, lineage: SubmissionLineage) -> None:
        self._by_attempt[lineage.submission_attempt_id] = lineage
        self._by_fingerprint.setdefault(lineage.request_fingerprint, []).append(
            lineage.submission_attempt_id
        )

    def get(self, attempt_id: str) -> SubmissionLineage | None:
        return self._by_attempt.get(attempt_id)

    def update(self, lineage: SubmissionLineage) -> None:
        self._by_attempt[lineage.submission_attempt_id] = lineage

    def ambiguous_for_fingerprint(self, fingerprint: str) -> bool:
        for aid in self._by_fingerprint.get(fingerprint, []):
            lin = self._by_attempt[aid]
            if lin.state is SubmissionAttemptState.AMBIGUOUS:
                return True
        return False

    def has_dispatched_or_open(self, fingerprint: str) -> bool:
        for aid in self._by_fingerprint.get(fingerprint, []):
            lin = self._by_attempt[aid]
            if lin.state in {
                SubmissionAttemptState.DISPATCHED,
                SubmissionAttemptState.ACKNOWLEDGED,
                SubmissionAttemptState.AMBIGUOUS,
                SubmissionAttemptState.RESOLVED,
            }:
                return True
        return False

    def to_list(self) -> list[dict[str, Any]]:
        return [v.to_dict() for v in self._by_attempt.values()]

    @classmethod
    def from_list(cls, rows: list[Mapping[str, Any]]) -> "LineageRegistry":
        reg = cls()
        for row in rows:
            lin = SubmissionLineage(
                intent_id=IntentId(str(row["intent_id"])),
                plan_id=PlanId(str(row["plan_id"])),
                request_fingerprint=str(row["request_fingerprint"]),
                submission_attempt_id=str(row["submission_attempt_id"]),
                local_order_id=(
                    None
                    if row.get("local_order_id") is None
                    else OrderId(str(row["local_order_id"]))
                ),
                venue_order_id=row.get("venue_order_id"),
                state=SubmissionAttemptState(str(row["state"])),
            )
            reg.register(lin)
        return reg
