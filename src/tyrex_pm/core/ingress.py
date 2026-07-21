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
    """Timing and provenance attached to a normalized ingress event."""

    receive_monotonic_ns: int
    clock_uncertainty_ms: int | None
    ingress_sequence: int
    connection_generation: int
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
