"""Provider-independent PTB attestation port (N3).

SSR/openPrice may be supplied as fixture-backed evidence for N3A.
No HTML scraping and no invented crypto PTB HTTP endpoint.
Mismatch tolerance remains OPEN — exact equality is reported; nonzero
tolerance is not applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.numerics import as_decimal
from tyrex_pm.domain.polymarket.boundary_candidates import BoundaryRuleId


class AttestationResult(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    INCOMPLETE = "INCOMPLETE"


class AttestationClassification(str, Enum):
    PROVEN = "PROVEN"
    PROVISIONAL = "PROVISIONAL"
    REFUTED = "REFUTED"
    OPEN = "OPEN"


@dataclass(frozen=True, kw_only=True)
class PtbAttestationRecord:
    """Normalized attestation comparison for one window candidate."""

    market_id: MarketId
    window_id: str
    candidate_value: Decimal | None
    attested_value: Decimal | None
    exact_diff: Decimal | None
    bps_diff: Decimal | None
    candidate_rule: BoundaryRuleId | None
    attestation_source: str
    attestation_provenance: Mapping[str, Any] = field(default_factory=dict)
    available_at: datetime | None
    result: AttestationResult
    classification: AttestationClassification
    mismatch_tolerance_bps: None = None  # OPEN — never invent a tolerance

    def __post_init__(self) -> None:
        if self.candidate_value is not None:
            object.__setattr__(
                self,
                "candidate_value",
                as_decimal(self.candidate_value, field_name="candidate_value"),
            )
        if self.attested_value is not None:
            object.__setattr__(
                self,
                "attested_value",
                as_decimal(self.attested_value, field_name="attested_value"),
            )
        if self.exact_diff is not None:
            object.__setattr__(
                self, "exact_diff", as_decimal(self.exact_diff, field_name="exact_diff")
            )
        if self.bps_diff is not None:
            object.__setattr__(
                self, "bps_diff", as_decimal(self.bps_diff, field_name="bps_diff")
            )
        if self.available_at is not None:
            object.__setattr__(
                self,
                "available_at",
                require_utc(self.available_at, field_name="available_at"),
            )
        object.__setattr__(
            self, "attestation_provenance", dict(self.attestation_provenance)
        )
        if self.mismatch_tolerance_bps is not None:
            raise ValueError(
                "mismatch_tolerance_bps must remain unset (OPEN) in N3A"
            )


def compare_attestation(
    *,
    market_id: MarketId,
    window_id: str,
    candidate_value: Decimal | str | None,
    attested_value: Decimal | str | None,
    candidate_rule: BoundaryRuleId | None,
    attestation_source: str,
    attestation_provenance: Mapping[str, Any] | None = None,
    available_at: datetime | None = None,
    classification_on_match: AttestationClassification = AttestationClassification.PROVISIONAL,
) -> PtbAttestationRecord:
    """Exact numeric compare — no invented tolerance."""
    if not attestation_source.strip():
        raise ValueError("attestation_source must be non-empty")
    if candidate_value is None or attested_value is None:
        return PtbAttestationRecord(
            market_id=market_id,
            window_id=window_id,
            candidate_value=None
            if candidate_value is None
            else as_decimal(candidate_value, field_name="candidate_value"),
            attested_value=None
            if attested_value is None
            else as_decimal(attested_value, field_name="attested_value"),
            exact_diff=None,
            bps_diff=None,
            candidate_rule=candidate_rule,
            attestation_source=attestation_source,
            attestation_provenance=dict(attestation_provenance or {}),
            available_at=available_at,
            result=AttestationResult.INCOMPLETE,
            classification=AttestationClassification.OPEN,
        )
    cand = as_decimal(candidate_value, field_name="candidate_value")
    att = as_decimal(attested_value, field_name="attested_value")
    if att <= 0:
        return PtbAttestationRecord(
            market_id=market_id,
            window_id=window_id,
            candidate_value=cand,
            attested_value=att,
            exact_diff=None,
            bps_diff=None,
            candidate_rule=candidate_rule,
            attestation_source=attestation_source,
            attestation_provenance=dict(attestation_provenance or {}),
            available_at=available_at,
            result=AttestationResult.INCOMPLETE,
            classification=AttestationClassification.OPEN,
        )
    diff = cand - att
    bps = (diff / att) * Decimal("10000")
    if diff == 0:
        return PtbAttestationRecord(
            market_id=market_id,
            window_id=window_id,
            candidate_value=cand,
            attested_value=att,
            exact_diff=diff,
            bps_diff=bps,
            candidate_rule=candidate_rule,
            attestation_source=attestation_source,
            attestation_provenance=dict(attestation_provenance or {}),
            available_at=available_at,
            result=AttestationResult.MATCH,
            classification=classification_on_match,
        )
    return PtbAttestationRecord(
        market_id=market_id,
        window_id=window_id,
        candidate_value=cand,
        attested_value=att,
        exact_diff=diff,
        bps_diff=bps,
        candidate_rule=candidate_rule,
        attestation_source=attestation_source,
        attestation_provenance=dict(attestation_provenance or {}),
        available_at=available_at,
        result=AttestationResult.MISMATCH,
        classification=AttestationClassification.REFUTED,
    )


@runtime_checkable
class PtbAttestationPort(Protocol):
    """Fetch independent PTB attestation evidence for a window."""

    def fetch_attestation(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        event_start: datetime,
    ) -> tuple[Decimal | None, str, Mapping[str, Any], datetime | None]:
        """Return (value, source_id, provenance, available_at)."""
        ...


@dataclass(frozen=True, kw_only=True)
class FixturePtbAttestationProvider:
    """Deterministic fixture/replay attestation for N3A."""

    # window_id -> (value, source, provenance)
    by_window: Mapping[str, tuple[Decimal | str, str, Mapping[str, Any]]]

    def fetch_attestation(
        self,
        *,
        market_id: MarketId,
        window_id: str,
        event_start: datetime,
    ) -> tuple[Decimal | None, str, Mapping[str, Any], datetime | None]:
        _ = market_id
        event_start = require_utc(event_start, field_name="event_start")
        row = self.by_window.get(window_id)
        if row is None:
            return None, "fixture_missing", {"window_id": window_id}, None
        value, source, prov = row
        return (
            as_decimal(value, field_name="attested_value"),
            source,
            dict(prov),
            event_start,
        )
