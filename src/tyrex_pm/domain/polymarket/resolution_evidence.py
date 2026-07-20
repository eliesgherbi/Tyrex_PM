"""Fixture/simulated binary resolution evidence (no network / redeem)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.instruments import OutcomeSide
from tyrex_pm.core.numerics import as_decimal
from tyrex_pm.domain.polymarket.resolution import BinaryResolutionRule, ComparisonRule


class ResolutionEvidenceStatus(str, Enum):
    READY = "READY"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    STALE = "STALE"


@dataclass(frozen=True, kw_only=True)
class ResolutionEvidence:
    """Explicit simulated resolution evidence for one market window."""

    market_id: MarketId
    window_id: str
    boundary_k: Decimal
    settlement_price: Decimal | None
    resolved_side: OutcomeSide | None  # YES=UP, NO=DOWN; None if unknown
    observed_at: datetime
    source: str
    provenance: str
    status: ResolutionEvidenceStatus = ResolutionEvidenceStatus.READY
    quality_note: str | None = None
    evidence_id: str = ""
    extras: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.window_id.strip():
            raise ValueError("window_id must be non-empty")
        if not self.source.strip():
            raise ValueError("source must be non-empty")
        object.__setattr__(
            self, "observed_at", require_utc(self.observed_at, field_name="observed_at")
        )
        object.__setattr__(
            self,
            "boundary_k",
            as_decimal(self.boundary_k, field_name="boundary_k"),
        )
        if self.settlement_price is not None:
            object.__setattr__(
                self,
                "settlement_price",
                as_decimal(self.settlement_price, field_name="settlement_price"),
            )
        object.__setattr__(self, "extras", dict(self.extras))


class ResolutionEvidenceError(ValueError):
    pass


def evaluate_outcome(
    *,
    settlement_price: Decimal,
    k: Decimal,
    rule: BinaryResolutionRule,
) -> OutcomeSide:
    """Reusable binary UP/DOWN outcome from settlement price vs K."""
    if rule.comparison is ComparisonRule.AT_OR_ABOVE_K:
        return OutcomeSide.YES if settlement_price >= k else OutcomeSide.NO
    if rule.comparison is ComparisonRule.STRICTLY_ABOVE_K:
        return OutcomeSide.YES if settlement_price > k else OutcomeSide.NO
    raise ResolutionEvidenceError(f"unsupported comparison {rule.comparison}")


def validate_resolution_evidence(
    evidence: ResolutionEvidence,
    *,
    market_id: MarketId,
    window_id: str,
    expected_k: Decimal | None,
    rule: BinaryResolutionRule | None = None,
) -> ResolutionEvidence:
    """Reject mismatched / incomplete evidence without guessing."""
    if evidence.market_id != market_id:
        raise ResolutionEvidenceError("market_id mismatch")
    if evidence.window_id != window_id:
        raise ResolutionEvidenceError("window_id mismatch")
    if expected_k is not None and evidence.boundary_k != as_decimal(
        expected_k, field_name="expected_k"
    ):
        raise ResolutionEvidenceError("boundary_k mismatch")
    if evidence.status is ResolutionEvidenceStatus.UNKNOWN:
        raise ResolutionEvidenceError("evidence status UNKNOWN")
    if evidence.status is ResolutionEvidenceStatus.STALE:
        raise ResolutionEvidenceError("evidence status STALE")
    if evidence.status is ResolutionEvidenceStatus.REJECTED:
        raise ResolutionEvidenceError("evidence status REJECTED")
    if evidence.settlement_price is None and evidence.resolved_side is None:
        raise ResolutionEvidenceError("incomplete evidence: no price or side")

    side = evidence.resolved_side
    if side is None and evidence.settlement_price is not None and rule is not None:
        side = evaluate_outcome(
            settlement_price=evidence.settlement_price, k=evidence.boundary_k, rule=rule
        )
        return ResolutionEvidence(
            market_id=evidence.market_id,
            window_id=evidence.window_id,
            boundary_k=evidence.boundary_k,
            settlement_price=evidence.settlement_price,
            resolved_side=side,
            observed_at=evidence.observed_at,
            source=evidence.source,
            provenance=evidence.provenance,
            status=ResolutionEvidenceStatus.READY,
            quality_note=evidence.quality_note,
            evidence_id=evidence.evidence_id,
            extras=evidence.extras,
        )
    if side is None:
        raise ResolutionEvidenceError("incomplete evidence: unresolved side")
    if side not in {OutcomeSide.YES, OutcomeSide.NO}:
        raise ResolutionEvidenceError("resolved_side must be YES or NO")
    return evidence


def simulated_payout_per_share(*, held_side: OutcomeSide, resolved_side: OutcomeSide) -> Decimal:
    """Binary payout: 1 if held side wins, else 0. Simulated/shadow only."""
    if held_side not in {OutcomeSide.YES, OutcomeSide.NO}:
        raise ResolutionEvidenceError("held_side must be YES or NO")
    return Decimal("1") if held_side is resolved_side else Decimal("0")
