"""Immutable sealed reference input for downstream consumers (N3).

Strategies must receive this (or equivalent sealed views), never mutable stores,
adapters, or raw provider payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.numerics import as_decimal
from tyrex_pm.domain.polymarket.boundary_candidates import (
    BoundaryRuleClassification,
    BoundaryRuleId,
)
from tyrex_pm.domain.polymarket.ptb_attestation import (
    AttestationClassification,
    AttestationResult,
)
from tyrex_pm.indicators.reference_alignment import AlignmentInitState


@dataclass(frozen=True, kw_only=True)
class SealedReferenceInput:
    """Immutable per-window reference package for later OBSERVE/SHADOW consumers."""

    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    ptb_k: Decimal
    boundary_rule_id: BoundaryRuleId
    boundary_classification: BoundaryRuleClassification
    ptb_attestation_result: AttestationResult
    ptb_attestation_classification: AttestationClassification
    trading_reference: Decimal | None
    trading_reference_identity: str | None
    trading_reference_source_ts: datetime | None
    aligned_chainlink_raw: Decimal | None
    aligned_binance_raw: Decimal | None
    basis_ln_instant: Decimal | None
    basis_ln_smoothed: Decimal | None
    c_hat: Decimal | None
    alignment_init_state: AlignmentInitState | None
    pairing_policy_id: str | None
    pairing_source_skew_ms: int | None
    chainlink_boundary_source_ts: datetime
    clock_status: str | None
    clock_uncertainty_ms: int | None
    clock_snapshot_id: str | None
    freshness_ready: bool
    readiness_ready: bool
    blocker_reasons: tuple[str, ...]
    sealed_at: datetime
    evidence_ids: tuple[str, ...] = ()
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_start", require_utc(self.event_start, field_name="event_start")
        )
        object.__setattr__(
            self, "event_end", require_utc(self.event_end, field_name="event_end")
        )
        object.__setattr__(
            self, "sealed_at", require_utc(self.sealed_at, field_name="sealed_at")
        )
        object.__setattr__(
            self,
            "chainlink_boundary_source_ts",
            require_utc(
                self.chainlink_boundary_source_ts,
                field_name="chainlink_boundary_source_ts",
            ),
        )
        object.__setattr__(self, "ptb_k", as_decimal(self.ptb_k, field_name="ptb_k"))
        if self.trading_reference is not None:
            object.__setattr__(
                self,
                "trading_reference",
                as_decimal(self.trading_reference, field_name="trading_reference"),
            )
        if self.trading_reference_source_ts is not None:
            object.__setattr__(
                self,
                "trading_reference_source_ts",
                require_utc(
                    self.trading_reference_source_ts,
                    field_name="trading_reference_source_ts",
                ),
            )
        object.__setattr__(self, "blocker_reasons", tuple(self.blocker_reasons))
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "provenance", dict(self.provenance))
