"""Provider-independent PTB/K snapshot and lock store."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import MarketId
from tyrex_pm.core.numerics import as_decimal


class PtbSourceClass(str, Enum):
    FIXTURE = "FIXTURE"
    SYNTHETIC = "SYNTHETIC"
    SETTLEMENT_BOUNDARY = "SETTLEMENT_BOUNDARY"
    UNKNOWN = "UNKNOWN"


class PtbQuality(str, Enum):
    """Quality classification for a K observation (F2 meanings)."""

    CONFIRMED_CANONICAL = "CONFIRMED_CANONICAL"
    PROVISIONAL = "PROVISIONAL"
    INFERRED = "INFERRED"
    LATE = "LATE"
    MISMATCHED = "MISMATCHED"
    MISSING = "MISSING"


@dataclass(frozen=True, kw_only=True)
class PtbSnapshot:
    """Immutable PTB/K observation for one market window.

    Provider-independent: fixtures and future adapters produce the same type.
    """

    market_id: MarketId
    window_id: str
    event_start: datetime
    event_end: datetime
    k: Decimal | None
    source_class: PtbSourceClass
    source_ts: datetime | None
    receive_ts: datetime
    boundary_lag_ms: int | None
    quality: PtbQuality
    locked: bool = False
    mismatch: bool = False
    mismatch_evidence: Mapping[str, Any] = field(default_factory=dict)
    provenance_ref: str | None = None
    readiness_reasons: tuple[str, ...] = ()
    attestation: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.window_id.strip():
            raise ValueError("window_id must be non-empty")
        object.__setattr__(
            self, "event_start", require_utc(self.event_start, field_name="event_start")
        )
        object.__setattr__(self, "event_end", require_utc(self.event_end, field_name="event_end"))
        object.__setattr__(
            self, "receive_ts", require_utc(self.receive_ts, field_name="receive_ts")
        )
        if self.source_ts is not None:
            object.__setattr__(
                self, "source_ts", require_utc(self.source_ts, field_name="source_ts")
            )
        if self.k is not None:
            object.__setattr__(self, "k", as_decimal(self.k, field_name="k"))
            if self.k <= 0:
                raise ValueError("k must be positive when present")
        object.__setattr__(self, "mismatch_evidence", dict(self.mismatch_evidence))
        object.__setattr__(self, "attestation", dict(self.attestation))
        object.__setattr__(self, "readiness_reasons", tuple(self.readiness_reasons))

    @property
    def usable_for_entry(self) -> bool:
        if self.k is None:
            return False
        if self.quality in {
            PtbQuality.MISSING,
            PtbQuality.MISMATCHED,
            PtbQuality.LATE,
        }:
            return False
        return self.quality in {
            PtbQuality.CONFIRMED_CANONICAL,
            PtbQuality.PROVISIONAL,
            PtbQuality.INFERRED,
        }


def make_fixture_ptb(
    *,
    market_id: MarketId,
    window_id: str,
    event_start: datetime,
    event_end: datetime,
    k: Decimal | str | None,
    receive_ts: datetime,
    source_ts: datetime | None = None,
    quality: PtbQuality = PtbQuality.CONFIRMED_CANONICAL,
    boundary_lag_ms: int | None = 0,
    provenance_ref: str = "fixture",
    locked: bool = False,
) -> PtbSnapshot:
    """Deterministic synthetic/fixture PTB construction for offline tests."""
    reasons: list[str] = []
    k_dec: Decimal | None
    if k is None:
        k_dec = None
        quality = PtbQuality.MISSING
        reasons.append("missing_k")
    else:
        k_dec = as_decimal(k, field_name="k")
    return PtbSnapshot(
        market_id=market_id,
        window_id=window_id,
        event_start=event_start,
        event_end=event_end,
        k=k_dec,
        source_class=PtbSourceClass.FIXTURE,
        source_ts=source_ts or receive_ts,
        receive_ts=receive_ts,
        boundary_lag_ms=boundary_lag_ms,
        quality=quality,
        locked=locked,
        provenance_ref=provenance_ref,
        readiness_reasons=tuple(reasons),
    )


@dataclass
class PtbLockStore:
    """Minimal in-memory K lock invariant (independent of Z-Gap policy).

    Once a K is locked for a window, conflicting later values produce mismatch
    evidence and do not mutate the locked snapshot.
    """

    _locked: dict[tuple[str, str], PtbSnapshot] = field(default_factory=dict)

    def get_locked(self, market_id: MarketId, window_id: str) -> PtbSnapshot | None:
        return self._locked.get((market_id.value, window_id))

    def lock(self, snapshot: PtbSnapshot) -> PtbSnapshot:
        if snapshot.k is None:
            raise ValueError("cannot lock PTB without K")
        key = (snapshot.market_id.value, snapshot.window_id)
        existing = self._locked.get(key)
        if existing is not None:
            return existing
        locked = PtbSnapshot(
            market_id=snapshot.market_id,
            window_id=snapshot.window_id,
            event_start=snapshot.event_start,
            event_end=snapshot.event_end,
            k=snapshot.k,
            source_class=snapshot.source_class,
            source_ts=snapshot.source_ts,
            receive_ts=snapshot.receive_ts,
            boundary_lag_ms=snapshot.boundary_lag_ms,
            quality=snapshot.quality,
            locked=True,
            mismatch=False,
            mismatch_evidence={},
            provenance_ref=snapshot.provenance_ref,
            readiness_reasons=snapshot.readiness_reasons,
            attestation=dict(snapshot.attestation),
        )
        self._locked[key] = locked
        return locked

    def observe(self, candidate: PtbSnapshot) -> PtbSnapshot:
        """Return locked snapshot, or candidate; mark mismatch without mutation."""
        key = (candidate.market_id.value, candidate.window_id)
        locked = self._locked.get(key)
        if locked is None:
            return candidate
        if candidate.k is None:
            return locked
        if locked.k is not None and candidate.k != locked.k:
            return PtbSnapshot(
                market_id=candidate.market_id,
                window_id=candidate.window_id,
                event_start=candidate.event_start,
                event_end=candidate.event_end,
                k=candidate.k,
                source_class=candidate.source_class,
                source_ts=candidate.source_ts,
                receive_ts=candidate.receive_ts,
                boundary_lag_ms=candidate.boundary_lag_ms,
                quality=PtbQuality.MISMATCHED,
                locked=False,
                mismatch=True,
                mismatch_evidence={
                    "locked_k": str(locked.k),
                    "candidate_k": str(candidate.k),
                    "locked_provenance": locked.provenance_ref,
                },
                provenance_ref=candidate.provenance_ref,
                readiness_reasons=("k_mismatch_vs_locked",),
                attestation=dict(candidate.attestation),
            )
        return locked
