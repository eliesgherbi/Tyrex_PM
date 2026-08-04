"""Common reporting envelope, lanes, health, and diagnostics registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol
from uuid import uuid4

SCHEMA_VERSION = "1.0"

_SECRET_KEY_FRAGMENTS = (
    "private_key",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "signature",
    "passphrase",
    "mnemonic",
    "seed",
    "token_secret",
)


class ReportingLane(str, Enum):
    CRITICAL = "critical"
    ANALYTICS = "analytics"


class ReportingHealth(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED_ANALYTICS = "DEGRADED_ANALYTICS"
    CRITICAL_AUDIT_FAILURE = "CRITICAL_AUDIT_FAILURE"


class RunLifecycle(str, Enum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    ABORTED = "ABORTED"


class EventFamily(str, Enum):
    DATA_HEALTH = "data_health"
    INDICATOR = "indicator"
    SIGNAL = "signal"
    DECISION = "decision"
    INTENT = "intent"
    RISK = "risk"
    PLAN = "plan"
    ORDER = "order"
    FILL = "fill"
    CANCEL = "cancel"
    POSITION = "position"
    LIFECYCLE = "lifecycle"
    EXIT = "exit"
    FLATTEN = "flatten"
    RECON = "recon"
    OPERATIONAL = "operational"
    REPORTING = "reporting"
    PRE_MUTATION = "pre_mutation"
    POST_MUTATION = "post_mutation"


@dataclass(frozen=True, slots=True)
class LineageIds:
    evaluation_id: str | None = None
    decision_id: str | None = None
    intent_id: str | None = None
    risk_decision_id: str | None = None
    execution_plan_id: str | None = None
    order_id: str | None = None
    fill_id: str | None = None
    position_id: str | None = None
    exit_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "evaluation_id": self.evaluation_id,
            "decision_id": self.decision_id,
            "intent_id": self.intent_id,
            "risk_decision_id": self.risk_decision_id,
            "execution_plan_id": self.execution_plan_id,
            "order_id": self.order_id,
            "fill_id": self.fill_id,
            "position_id": self.position_id,
            "exit_id": self.exit_id,
        }


@dataclass(frozen=True, slots=True)
class StrategyDiagnosticsBlob:
    namespace: str
    schema_version: str
    values: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "schema_version": self.schema_version,
            "values": redact_mapping(dict(self.values)),
        }


@dataclass(frozen=True, slots=True)
class ReportingEvent:
    """Common reporting envelope (schema_version owned here)."""

    schema_version: str
    event_id: str
    run_id: str
    mode: str
    sequence: int
    event_family: str
    event_type: str
    event_time: datetime
    record_time: datetime
    producer: str
    lane: ReportingLane
    payload: Mapping[str, Any] = field(default_factory=dict)
    strategy_id: str | None = None
    strategy_version: str | None = None
    market_id: str | None = None
    window_id: str | None = None
    severity: str = "info"
    correlation_id: str | None = None
    causation_id: str | None = None
    lineage: LineageIds = field(default_factory=LineageIds)
    strategy_diagnostics: StrategyDiagnosticsBlob | None = None

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if self.sequence < 1:
            raise ValueError("sequence must be >= 1")
        if not self.event_id.strip():
            raise ValueError("event_id required")
        if not self.run_id.strip():
            raise ValueError("run_id required")
        if not self.event_family.strip() or not self.event_type.strip():
            raise ValueError("event_family and event_type required")
        if not self.producer.strip():
            raise ValueError("producer required")
        object.__setattr__(self, "payload", redact_mapping(dict(self.payload)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "mode": self.mode,
            "sequence": self.sequence,
            "event_family": self.event_family,
            "event_type": self.event_type,
            "event_time": _iso(self.event_time),
            "record_time": _iso(self.record_time),
            "producer": self.producer,
            "lane": self.lane.value,
            "severity": self.severity,
            "market_id": self.market_id,
            "window_id": self.window_id,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "lineage": self.lineage.to_dict(),
            "payload": dict(self.payload),
            "strategy_diagnostics": None
            if self.strategy_diagnostics is None
            else self.strategy_diagnostics.to_dict(),
        }


class DiagnosticsContract(Protocol):
    """Composition-supplied strategy diagnostics registration (no core import list)."""

    namespace: str
    schema_version: str
    strategy_id: str
    strategy_version: str

    def build_diagnostics(self, context: Mapping[str, Any]) -> StrategyDiagnosticsBlob: ...

    def build_gates(self, context: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    def closest_candidate_fields(
        self, context: Mapping[str, Any]
    ) -> dict[str, Any] | None: ...


def new_event_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def redact_mapping(data: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        k = str(key)
        if _is_secret_key(k):
            out[k] = "[REDACTED]"
            continue
        if isinstance(value, Mapping):
            out[k] = redact_mapping(value)
        elif isinstance(value, list):
            out[k] = [
                redact_mapping(v) if isinstance(v, Mapping) else v for v in value
            ]
        else:
            out[k] = value
    return out


def _is_secret_key(key: str) -> bool:
    lowered = key.lower().replace("-", "_")
    return any(frag in lowered for frag in _SECRET_KEY_FRAGMENTS)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def default_lane_for_family(family: str, *, force_critical: bool = False) -> ReportingLane:
    if force_critical:
        return ReportingLane.CRITICAL
    critical = {
        EventFamily.INTENT.value,
        EventFamily.RISK.value,
        EventFamily.PLAN.value,
        EventFamily.ORDER.value,
        EventFamily.FILL.value,
        EventFamily.CANCEL.value,
        EventFamily.POSITION.value,
        EventFamily.LIFECYCLE.value,
        EventFamily.EXIT.value,
        EventFamily.FLATTEN.value,
        EventFamily.RECON.value,
        EventFamily.OPERATIONAL.value,
        EventFamily.REPORTING.value,
        EventFamily.PRE_MUTATION.value,
        EventFamily.POST_MUTATION.value,
    }
    return ReportingLane.CRITICAL if family in critical else ReportingLane.ANALYTICS
