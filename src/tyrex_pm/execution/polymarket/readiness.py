"""Live execution readiness — fail closed until all prerequisites hold."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ReadinessReason(str, Enum):
    READY = "READY"
    CONFIG_INVALID = "CONFIG_INVALID"
    CREDENTIALS_MISSING = "CREDENTIALS_MISSING"
    MARKET_UNRESOLVED = "MARKET_UNRESOLVED"
    PERSISTENCE_NOT_LOADED = "PERSISTENCE_NOT_LOADED"
    TRANSPORT_DISCONNECTED = "TRANSPORT_DISCONNECTED"
    PUBLIC_TIME_INVALID = "PUBLIC_TIME_INVALID"
    USER_STREAM_UNREADY = "USER_STREAM_UNREADY"
    USER_STREAM_INIT_FAILED = "USER_STREAM_INIT_FAILED"
    ADAPTER_CONTRACT_FAILURE = "ADAPTER_CONTRACT_FAILURE"
    RECONCILIATION_PENDING = "RECONCILIATION_PENDING"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    BOOKS_STALE = "BOOKS_STALE"
    BALANCE_UNKNOWN = "BALANCE_UNKNOWN"
    UNRESOLVED_MISMATCH = "UNRESOLVED_MISMATCH"
    UNKNOWN_SUBMISSION = "UNKNOWN_SUBMISSION"
    MUTATIONS_DISABLED = "MUTATIONS_DISABLED"
    LIVE_TINY_DISPATCH_DISABLED = "LIVE_TINY_DISPATCH_DISABLED"


@dataclass
class ExecutionReadiness:
    ready: bool = False
    reasons: list[ReadinessReason] = field(default_factory=list)
    mutations_enabled: bool = False

    def deny(self, reason: ReadinessReason) -> None:
        self.ready = False
        if reason not in self.reasons:
            self.reasons.append(reason)

    def clear(self, reason: ReadinessReason) -> None:
        self.reasons = [r for r in self.reasons if r is not reason]
        self._recompute()

    def set_reasons(self, reasons: list[ReadinessReason]) -> None:
        self.reasons = list(reasons)
        self._recompute()

    def _recompute(self) -> None:
        blocking = [r for r in self.reasons if r is not ReadinessReason.READY]
        self.ready = len(blocking) == 0

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "mutations_enabled": self.mutations_enabled,
            "reasons": [r.value for r in self.reasons],
        }
