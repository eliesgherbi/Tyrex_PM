"""Decision-time freshness assessment (not a latched store flag)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tyrex_pm.core.clock import Clock, require_utc


class TimestampBasis(str, Enum):
    EVENT_TIME = "EVENT_TIME"
    RECEIVE_TIME = "RECEIVE_TIME"


class FreshnessReason(str, Enum):
    FRESH = "FRESH"
    UNINITIALIZED = "UNINITIALIZED"
    STALE = "STALE"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"


@dataclass(frozen=True, kw_only=True)
class FreshnessAssessment:
    is_fresh: bool
    age_ms: int | None
    threshold_ms: int
    timestamp_basis: TimestampBasis
    reason_code: FreshnessReason
    observed_at: datetime


@dataclass(frozen=True, kw_only=True)
class FreshnessConfig:
    book_threshold_ms: int
    reference_threshold_ms: int
    future_tolerance_ms: int = 500
    timestamp_basis: TimestampBasis = TimestampBasis.EVENT_TIME

    def __post_init__(self) -> None:
        if self.book_threshold_ms <= 0 or self.reference_threshold_ms <= 0:
            raise ValueError("freshness thresholds must be > 0")
        if self.future_tolerance_ms < 0:
            raise ValueError("future_tolerance_ms must be >= 0")


def assess_freshness(
    *,
    clock: Clock,
    ts_event: datetime | None,
    ts_received: datetime | None,
    initialized: bool,
    threshold_ms: int,
    future_tolerance_ms: int,
    basis: TimestampBasis,
) -> FreshnessAssessment:
    now = require_utc(clock.now_utc(), field_name="now")
    if not initialized:
        return FreshnessAssessment(
            is_fresh=False,
            age_ms=None,
            threshold_ms=threshold_ms,
            timestamp_basis=basis,
            reason_code=FreshnessReason.UNINITIALIZED,
            observed_at=now,
        )
    stamp = ts_event if basis is TimestampBasis.EVENT_TIME else ts_received
    if stamp is None:
        return FreshnessAssessment(
            is_fresh=False,
            age_ms=None,
            threshold_ms=threshold_ms,
            timestamp_basis=basis,
            reason_code=FreshnessReason.MISSING_TIMESTAMP,
            observed_at=now,
        )
    stamp = require_utc(stamp, field_name="stamp")
    delta = now - stamp
    age_ms = int(delta.total_seconds() * 1000)
    if age_ms < -future_tolerance_ms:
        return FreshnessAssessment(
            is_fresh=False,
            age_ms=age_ms,
            threshold_ms=threshold_ms,
            timestamp_basis=basis,
            reason_code=FreshnessReason.FUTURE_TIMESTAMP,
            observed_at=now,
        )
    if age_ms > threshold_ms:
        return FreshnessAssessment(
            is_fresh=False,
            age_ms=age_ms,
            threshold_ms=threshold_ms,
            timestamp_basis=basis,
            reason_code=FreshnessReason.STALE,
            observed_at=now,
        )
    return FreshnessAssessment(
        is_fresh=True,
        age_ms=max(0, age_ms),
        threshold_ms=threshold_ms,
        timestamp_basis=basis,
        reason_code=FreshnessReason.FRESH,
        observed_at=now,
    )
