"""Outcome labels for strategy replay: qualified when supported, otherwise explicit proxy.

Recorded resolution rows carry ``payouts`` such as ``[1000000, 0]`` with
``payout_unit = UNKNOWN``. The warehouse correctly refuses to qualify them. For
research only, a *proxy outcome* is derived: when exactly one outcome has a
positive payout entry and every other entry is zero, that outcome is taken as the
winner. This reads which index is nonzero; it does not claim a payout unit, a
finality guarantee, or the settlement rule. Results carry ``label_type`` so proxy
outcomes are never presented as qualified settlement labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .loader import WindowData

PROXY_METHOD = "nonzero_payout_entry_index_v1"
PROXY_STATUS = "PROXY_OUTCOME"


@dataclass(frozen=True)
class OutcomeLabel:
    label_type: str  # "proxy_outcome" | "unavailable"
    winner: str | None  # "UP" | "DOWN"
    reason: str
    method: str
    provider_status: str | None = None
    payout_vector: tuple[str, ...] = ()
    payout_unit: str | None = None
    resolved_at_ns: int | None = None
    available_at_ns: int | None = None
    transaction_hash: str | None = None

    @property
    def available(self) -> bool:
        return self.winner is not None


def proxy_outcome(window: WindowData) -> OutcomeLabel:
    resolved = [r for r in window.resolution_rows if r.get("provider_status") == "resolved"]
    if not resolved:
        statuses = sorted({str(r.get("provider_status")) for r in window.resolution_rows})
        return OutcomeLabel(
            label_type="unavailable",
            winner=None,
            reason="no_resolved_observation" + (f":{','.join(statuses)}" if statuses else ""),
            method=PROXY_METHOD,
        )
    vectors = {tuple(Decimal(str(p)) for p in r.get("payouts") or ()) for r in resolved}
    if len(vectors) != 1:
        return OutcomeLabel(
            label_type="unavailable",
            winner=None,
            reason="conflicting_payout_vectors",
            method=PROXY_METHOD,
        )
    vector = next(iter(vectors))
    first = min(resolved, key=lambda r: r["received_at"])
    positive = [i for i, p in enumerate(vector) if p > 0]
    if len(vector) != 2 or len(positive) != 1 or any(p < 0 for p in vector):
        return OutcomeLabel(
            label_type="unavailable",
            winner=None,
            reason="payout_vector_not_single_winner",
            method=PROXY_METHOD,
            provider_status="resolved",
            payout_vector=tuple(str(p) for p in vector),
        )
    index = positive[0]
    if index == window.up_outcome_index:
        winner = "UP"
    elif index == window.down_outcome_index:
        winner = "DOWN"
    else:
        return OutcomeLabel(
            label_type="unavailable",
            winner=None,
            reason="payout_index_not_mapped",
            method=PROXY_METHOD,
        )
    return OutcomeLabel(
        label_type="proxy_outcome",
        winner=winner,
        reason="single_positive_payout_entry",
        method=PROXY_METHOD,
        provider_status="resolved",
        payout_vector=tuple(str(p) for p in vector),
        payout_unit=first.get("payout_unit"),
        resolved_at_ns=first.get("resolved_at"),
        available_at_ns=first["received_at"],
        transaction_hash=first.get("transaction_hash"),
    )


def engine_labels(window: WindowData, label: OutcomeLabel) -> list[dict]:
    """Per-instrument labels for ``Engine.run_closed_loop`` (status PROXY_OUTCOME)."""
    if not label.available or label.available_at_ns is None:
        return []
    return [
        {
            "instrument_id": window.up_instrument_id,
            "status": PROXY_STATUS,
            "payout": "1" if label.winner == "UP" else "0",
            "available_at": label.available_at_ns,
        },
        {
            "instrument_id": window.down_instrument_id,
            "status": PROXY_STATUS,
            "payout": "1" if label.winner == "DOWN" else "0",
            "available_at": label.available_at_ns,
        },
    ]
