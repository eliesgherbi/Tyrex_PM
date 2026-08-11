"""Sealed per-window PTB versus dynamic aligned-reference snapshots.

``SealedWindowPtb`` is immutable for the window once sealed (K never changes).
``DynamicAlignedReference`` is rebuilt on every evaluation from current Binance
and the latest causally accepted basis estimate — it must not be frozen into
the sealed PTB package.
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
from tyrex_pm.indicators.reference_alignment import AlignmentInitState, AlignmentMode


@dataclass(frozen=True, kw_only=True)
class SealedWindowPtb:
    """Immutable sealed K / window identity for downstream consumers."""

    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    ptb_k: Decimal
    boundary_rule_id: BoundaryRuleId
    boundary_classification: BoundaryRuleClassification
    ptb_attestation_result: AttestationResult
    ptb_attestation_classification: AttestationClassification
    chainlink_boundary_source_ts: datetime
    chainlink_boundary_value: Decimal
    boundary_lag_ms: int | None
    clock_status: str | None
    clock_uncertainty_ms: int | None
    clock_snapshot_id: str | None
    sealed_at: datetime
    evidence_ids: tuple[str, ...] = ()
    blocker_reasons: tuple[str, ...] = ()
    readiness_ready: bool = False
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "event_start", require_utc(self.event_start, field_name="event_start")
        )
        object.__setattr__(self, "event_end", require_utc(self.event_end, field_name="event_end"))
        object.__setattr__(self, "sealed_at", require_utc(self.sealed_at, field_name="sealed_at"))
        object.__setattr__(
            self,
            "chainlink_boundary_source_ts",
            require_utc(
                self.chainlink_boundary_source_ts,
                field_name="chainlink_boundary_source_ts",
            ),
        )
        object.__setattr__(self, "ptb_k", as_decimal(self.ptb_k, field_name="ptb_k"))
        object.__setattr__(
            self,
            "chainlink_boundary_value",
            as_decimal(self.chainlink_boundary_value, field_name="chainlink_boundary_value"),
        )
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "blocker_reasons", tuple(self.blocker_reasons))
        object.__setattr__(self, "provenance", dict(self.provenance))


@dataclass(frozen=True, kw_only=True)
class DynamicAlignedReference:
    """Immutable per-evaluation dynamic reference view (never sealed into K)."""

    binance_raw: Decimal
    binance_source_ts: datetime
    trading_identity: str
    chainlink_raw: Decimal | None
    chainlink_source_ts: datetime | None
    instantaneous_basis_ln: Decimal | None
    basis_estimate_used_ln: Decimal | None
    basis_estimate_as_of_ts: datetime | None
    basis_estimate_includes_current_chainlink: bool
    c_hat: Decimal | None
    alignment_mode: AlignmentMode
    pairing_policy_id: str
    pairing_source_skew_ms: int | None
    init_state: AlignmentInitState
    clock_status: str | None
    blocker_reasons: tuple[str, ...] = ()
    ewma_half_life_s: float | None = None
    evaluated_at: datetime | None = None
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "binance_source_ts",
            require_utc(self.binance_source_ts, field_name="binance_source_ts"),
        )
        object.__setattr__(
            self, "binance_raw", as_decimal(self.binance_raw, field_name="binance_raw")
        )
        if self.chainlink_raw is not None:
            object.__setattr__(
                self,
                "chainlink_raw",
                as_decimal(self.chainlink_raw, field_name="chainlink_raw"),
            )
        if self.chainlink_source_ts is not None:
            object.__setattr__(
                self,
                "chainlink_source_ts",
                require_utc(self.chainlink_source_ts, field_name="chainlink_source_ts"),
            )
        if self.basis_estimate_as_of_ts is not None:
            object.__setattr__(
                self,
                "basis_estimate_as_of_ts",
                require_utc(self.basis_estimate_as_of_ts, field_name="basis_estimate_as_of_ts"),
            )
        if self.evaluated_at is not None:
            object.__setattr__(
                self,
                "evaluated_at",
                require_utc(self.evaluated_at, field_name="evaluated_at"),
            )
        object.__setattr__(self, "blocker_reasons", tuple(self.blocker_reasons))
        object.__setattr__(self, "provenance", dict(self.provenance))


# Backward-compatible name used by N3 seal path: sealed window PTB only.
SealedReferenceInput = SealedWindowPtb
