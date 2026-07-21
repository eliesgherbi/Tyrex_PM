"""Append-only ingress provenance and late/OOO retention (N2).

Adapters emit normalized events; hosts may wrap them in ``IngressRecord`` for
audit. Current-view stores may reject stale updates only *after* recording.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from tyrex_pm.core.clock import require_utc


class FeedRole(str, Enum):
    """Semantic role of a price feed — never conflate Binance with settlement."""

    SETTLEMENT_REFERENCE = "settlement_reference"
    TRADING_REFERENCE = "trading_reference"
    COMPARISON_REFERENCE = "comparison_reference"
    MARKET_BOOK = "market_book"
    CLOCK = "clock"


class FeedReadiness(str, Enum):
    NOT_READY = "NOT_READY"
    READY = "READY"
    STALE = "STALE"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True, kw_only=True)
class IngressMeta:
    """Timing and provenance attached to a normalized ingress event.

    Wall-clock semantics (N2 correction):

    * ``receive_wall_raw_utc`` — host OS wall UTC observed at ingress (uncorrected).
    * ``receive_wall_corrected_utc`` — raw + ``clock_offset_ms`` from TimeAuthority.
    * ``Event.ts_received`` on the parent event **must** equal ``receive_wall_raw_utc``.
      Adapters must never write corrected time into ``Event.ts_received``.
    """

    receive_monotonic_ns: int
    ingress_sequence: int
    connection_generation: int
    receive_wall_raw_utc: datetime | None = None
    receive_wall_corrected_utc: datetime | None = None
    clock_offset_ms: float | None = None
    clock_uncertainty_ms: int | None = None
    clock_status: str | None = None
    clock_snapshot_id: str | None = None
    provider_sequence_id: str | None = None
    raw_fingerprint: str = ""
    late_or_out_of_order: str | None = None
    role: FeedRole = FeedRole.TRADING_REFERENCE
    subscription_mode: str | None = None

    def __post_init__(self) -> None:
        if self.ingress_sequence < 1:
            raise ValueError("ingress_sequence must be >= 1")
        if self.connection_generation < 1:
            raise ValueError("connection_generation must be >= 1")
        if self.receive_monotonic_ns < 0:
            raise ValueError("receive_monotonic_ns must be >= 0")
        if self.clock_uncertainty_ms is not None and self.clock_uncertainty_ms < 0:
            raise ValueError("clock_uncertainty_ms must be >= 0 when set")
        if self.receive_wall_raw_utc is not None:
            object.__setattr__(
                self,
                "receive_wall_raw_utc",
                require_utc(self.receive_wall_raw_utc, field_name="receive_wall_raw_utc"),
            )
        if self.receive_wall_corrected_utc is not None:
            object.__setattr__(
                self,
                "receive_wall_corrected_utc",
                require_utc(
                    self.receive_wall_corrected_utc,
                    field_name="receive_wall_corrected_utc",
                ),
            )


def build_ingress_timing(
    *,
    receive_wall_raw_utc: datetime,
    receive_monotonic_ns: int,
    ingress_sequence: int,
    connection_generation: int,
    time_view: Any | None = None,
    provider_sequence_id: str | None = None,
    raw_fingerprint: str = "",
    late_or_out_of_order: str | None = None,
    role: FeedRole = FeedRole.TRADING_REFERENCE,
    subscription_mode: str | None = None,
    clock_snapshot_id: str | None = None,
) -> IngressMeta:
    """Build IngressMeta with explicit raw vs corrected receive walls.

    ``time_view`` is a ``TimeAuthorityView`` (or compatible). When absent,
    corrected wall equals raw and clock_status is UNSYNCHRONIZED.
    """
    from datetime import timedelta

    from tyrex_pm.core.time_authority import TimeSyncStatus

    raw = require_utc(receive_wall_raw_utc, field_name="receive_wall_raw_utc")
    if time_view is None:
        return IngressMeta(
            receive_monotonic_ns=receive_monotonic_ns,
            ingress_sequence=ingress_sequence,
            connection_generation=connection_generation,
            receive_wall_raw_utc=raw,
            receive_wall_corrected_utc=raw,
            clock_offset_ms=None,
            clock_uncertainty_ms=None,
            clock_status=TimeSyncStatus.UNSYNCHRONIZED.value,
            clock_snapshot_id=clock_snapshot_id,
            provider_sequence_id=provider_sequence_id,
            raw_fingerprint=raw_fingerprint,
            late_or_out_of_order=late_or_out_of_order,
            role=role,
            subscription_mode=subscription_mode,
        )
    offset = float(getattr(time_view, "estimated_offset_ms", 0.0) or 0.0)
    corrected = raw + timedelta(milliseconds=offset)
    status = getattr(time_view, "sync_status", TimeSyncStatus.UNSYNCHRONIZED)
    status_s = status.value if hasattr(status, "value") else str(status)
    snap_id = clock_snapshot_id or getattr(time_view, "clock_snapshot_id", None)
    return IngressMeta(
        receive_monotonic_ns=receive_monotonic_ns,
        ingress_sequence=ingress_sequence,
        connection_generation=connection_generation,
        receive_wall_raw_utc=raw,
        receive_wall_corrected_utc=corrected,
        clock_offset_ms=offset,
        clock_uncertainty_ms=int(getattr(time_view, "uncertainty_ms", 0) or 0),
        clock_status=status_s,
        clock_snapshot_id=snap_id,
        provider_sequence_id=provider_sequence_id,
        raw_fingerprint=raw_fingerprint,
        late_or_out_of_order=late_or_out_of_order,
        role=role,
        subscription_mode=subscription_mode,
    )


def fingerprint_payload(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True, kw_only=True)
class IngressRecord:
    """Append-only audit row — retained even when current view rejects the update."""

    source: str
    symbol: str
    role: FeedRole
    source_ts: datetime | None
    receive_wall_utc: datetime
    value: str | None
    meta: IngressMeta
    accepted_by_current_view: bool
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "receive_wall_utc",
            require_utc(self.receive_wall_utc, field_name="receive_wall_utc"),
        )
        if self.source_ts is not None:
            object.__setattr__(
                self, "source_ts", require_utc(self.source_ts, field_name="source_ts")
            )


@dataclass
class AppendOnlyIngressLog:
    """In-memory append-only ingress evidence (process-local)."""

    _rows: list[IngressRecord] = field(default_factory=list)

    def append(self, record: IngressRecord) -> IngressRecord:
        self._rows.append(record)
        return record

    @property
    def rows(self) -> tuple[IngressRecord, ...]:
        return tuple(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


@dataclass
class IngressSequencer:
    """Monotonic ingress sequence generator (per process / supervisor)."""

    _next: int = 1

    def next(self) -> int:
        seq = self._next
        self._next += 1
        return seq


@dataclass
class ConnectionGeneration:
    """Increments on each successful (re)connect."""

    value: int = 0

    def bump(self) -> int:
        self.value += 1
        return self.value
