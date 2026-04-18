from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from tyrex_pm.core.enums import Side
from tyrex_pm.core.ids import ClientOrderId, TokenId, VenueOrderId


@dataclass
class LocalOrder:
    """OMS-local row: provisional until user WS or REST repair confirms venue snapshot.

    Repair lifecycle (see ``state.reconcile``):
        provisional -> venue_confirmed   (WS / REST shows it open)
        provisional -> filled_resolved   (WS trade evidence covers original size)
        provisional -> unknown_terminal  (absent past timeout, WS fresh, no restart) → row dropped, audit kept
    """

    client_order_id: ClientOrderId
    venue_order_id: VenueOrderId | None
    token_id: TokenId
    side: Side
    remaining: Decimal
    original_size: Decimal | None = None
    size_matched: Decimal | None = None
    #: ``provisional`` | ``venue_confirmed`` (terminal states never persist on the row).
    confirmation: str = "provisional"
    submit_ack_utc: datetime | None = None
    #: ``local`` | ``user_ws`` | ``rest`` — last channel that aligned size fields.
    last_local_source: str = "local"
    #: Stable hash of (token_id|side|size|limit_price); duplicate-submit guard while repair is pending.
    submit_fingerprint: str | None = None
    #: Venue ack status string (e.g. ``"live"`` | ``"matched"`` | ``"delayed"``); aids repair triage.
    ack_status: str | None = None
    #: Number of repair probes that have observed this row absent from merged book (informational).
    repair_attempts: int = 0
    #: Limit price at submit time. Used by the venue-adoption matcher to link unlinked venue orders
    #: back to a recent local provisional row (REST-ahead-of-local-registration race).
    limit_price: Decimal | None = None
    #: When the local row was first ``register_submit``ed. Drives adoption age window when
    #: ``submit_ack_utc`` is not yet set.
    register_utc: datetime | None = None


def compute_submit_fingerprint(
    *,
    token_id: TokenId | str,
    side: Side | str,
    size: Decimal,
    limit_price: Decimal | None,
) -> str:
    """Stable id for an in-flight submission so a follow-up signal cannot blindly resubmit."""
    side_v = side.value if isinstance(side, Side) else str(side)
    px = "MKT" if limit_price is None else f"{limit_price:.10f}".rstrip("0").rstrip(".")
    sz = f"{size:.10f}".rstrip("0").rstrip(".") or "0"
    raw = f"{token_id}|{side_v}|{sz}|{px}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class OrderStore:
    """Local OMS + venue mapping (provisional repair lifecycle lives here)."""

    orders: dict[ClientOrderId, LocalOrder] = field(default_factory=dict)
    in_flight_by_token: dict[TokenId, Decimal] = field(default_factory=dict)
    in_flight_order_count: int = 0
    #: Active submit fingerprints (provisional rows) — duplicate-submit guard.
    pending_repair_fingerprints: set[str] = field(default_factory=set)
    #: Durable audit trail of repair-decided terminal resolutions (filled_resolved / unknown_terminal).
    terminal_audit: list[dict[str, Any]] = field(default_factory=list)

    def has_pending_submit_fingerprint(self, fp: str) -> bool:
        return fp in self.pending_repair_fingerprints

    def record_terminal_audit(self, entry: dict[str, Any]) -> None:
        self.terminal_audit.append(entry)
        if len(self.terminal_audit) > 1024:
            self.terminal_audit = self.terminal_audit[-1024:]
