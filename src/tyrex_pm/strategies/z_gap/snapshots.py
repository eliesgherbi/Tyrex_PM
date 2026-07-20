"""Immutable Z-Gap model snapshots and atomic decision-epoch metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping
from uuid import uuid4

from tyrex_pm.core.clock import require_utc
from tyrex_pm.core.ids import CorrelationId, EventId, MarketId
from tyrex_pm.core.numerics import as_decimal
from tyrex_pm.domain.polymarket.ptb import PtbSnapshot
from tyrex_pm.strategies.z_gap.reasons import ZGapReason


@dataclass(frozen=True, kw_only=True)
class DecisionEpoch:
    """Sealed identity for one atomic decision evaluation."""

    epoch_id: str
    market_id: MarketId
    window_id: str
    evaluated_at: datetime
    correlation_id: CorrelationId
    causation_id: EventId | None = None
    snapshot_version: int = 1

    def __post_init__(self) -> None:
        if not self.epoch_id.strip():
            raise ValueError("epoch_id must be non-empty")
        if not self.window_id.strip():
            raise ValueError("window_id must be non-empty")
        object.__setattr__(
            self,
            "evaluated_at",
            require_utc(self.evaluated_at, field_name="evaluated_at"),
        )

    @staticmethod
    def new(
        *,
        market_id: MarketId,
        window_id: str,
        evaluated_at: datetime,
        correlation_id: CorrelationId,
        causation_id: EventId | None = None,
        snapshot_version: int = 1,
    ) -> DecisionEpoch:
        return DecisionEpoch(
            epoch_id=str(uuid4()),
            market_id=market_id,
            window_id=window_id,
            evaluated_at=evaluated_at,
            correlation_id=correlation_id,
            causation_id=causation_id,
            snapshot_version=snapshot_version,
        )


def require_compatible_epoch(
    epoch: DecisionEpoch,
    *,
    other: DecisionEpoch | None = None,
    market_id: MarketId | None = None,
    window_id: str | None = None,
) -> ZGapReason | None:
    """Return a reason if components are incompatible; else None."""
    if other is not None and other.epoch_id != epoch.epoch_id:
        return ZGapReason.EPOCH_MISMATCH
    if market_id is not None and market_id != epoch.market_id:
        return ZGapReason.WINDOW_MISMATCH
    if window_id is not None and window_id != epoch.window_id:
        return ZGapReason.WINDOW_MISMATCH
    return None


@dataclass(frozen=True, kw_only=True)
class ZGapModelSnapshot:
    """Immutable model state for one sealed decision epoch."""

    epoch: DecisionEpoch
    S: Decimal | None
    K: Decimal | None
    sigma: float | None
    sigma_units: str
    tau_s: float | None
    z: float | None
    p_up: float | None
    p_down: float | None
    basis_bps: Decimal | None
    ptb: PtbSnapshot | None
    ready: bool
    jump_guard_tripped: bool = False
    reject_reasons: tuple[str, ...] = ()
    source_timestamps: Mapping[str, datetime] = field(default_factory=dict)
    freshness: Mapping[str, bool] = field(default_factory=dict)
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.S is not None:
            object.__setattr__(self, "S", as_decimal(self.S, field_name="S"))
        if self.K is not None:
            object.__setattr__(self, "K", as_decimal(self.K, field_name="K"))
        if self.basis_bps is not None:
            object.__setattr__(
                self, "basis_bps", as_decimal(self.basis_bps, field_name="basis_bps")
            )
        object.__setattr__(self, "reject_reasons", tuple(self.reject_reasons))
        object.__setattr__(self, "source_timestamps", dict(self.source_timestamps))
        object.__setattr__(self, "freshness", dict(self.freshness))
        object.__setattr__(self, "evidence", dict(self.evidence))

    def p_held(self, leg: str) -> float | None:
        if leg == "UP":
            return self.p_up
        if leg == "DOWN":
            return self.p_down
        raise ValueError(f"unknown leg: {leg!r}")


def build_model_snapshot(
    *,
    epoch: DecisionEpoch,
    fair_value_status: str,
    fair_value_reject: str | None,
    S: Decimal | None,
    K: Decimal | None,
    sigma: float | None,
    sigma_units: str,
    tau_s: float | None,
    z: float | None,
    p_up: float | None,
    p_down: float | None,
    basis_bps: Decimal | None,
    ptb: PtbSnapshot | None,
    jump_guard_tripped: bool = False,
    extra_reasons: tuple[str, ...] = (),
    source_timestamps: Mapping[str, datetime] | None = None,
    freshness: Mapping[str, bool] | None = None,
) -> ZGapModelSnapshot:
    """Compose an immutable model snapshot for one sealed epoch (pure)."""
    reasons: list[str] = list(extra_reasons)
    ready = fair_value_status == "ready" and not jump_guard_tripped
    if not ready and fair_value_reject:
        reasons.append(fair_value_reject)
    if jump_guard_tripped:
        reasons.append("jump_guard_tripped")
        ready = False
    return ZGapModelSnapshot(
        epoch=epoch,
        S=S,
        K=K,
        sigma=sigma,
        sigma_units=sigma_units,
        tau_s=tau_s,
        z=z,
        p_up=p_up,
        p_down=p_down,
        basis_bps=basis_bps,
        ptb=ptb,
        ready=ready,
        jump_guard_tripped=jump_guard_tripped,
        reject_reasons=tuple(reasons),
        source_timestamps=source_timestamps or {},
        freshness=freshness or {},
    )
