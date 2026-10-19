"""Recording configuration (``recording:`` in run and observation configs)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping


class RecordingLevel(str, Enum):
    ESSENTIAL = "essential"  # operational evidence only; no per-decision records
    DECISIONS = "decisions"  # + one compact record per evaluation
    EXPLAIN = "explain"  # + calculated state behind decisions, by trigger

    def includes(self, other: "RecordingLevel") -> bool:
        order = (RecordingLevel.ESSENTIAL, RecordingLevel.DECISIONS, RecordingLevel.EXPLAIN)
        return order.index(self) >= order.index(other)


class ExplainTrigger(str, Enum):
    ALL = "all"  # every evaluation (short diagnostic runs)
    ENTRY_BAND = "entry_band"  # inside the strategy's entry band + actionable events
    ACTIONABLE = "actionable"  # entries, vetoes, holds/exits and data faults only


OVERFLOW_POLICIES = frozenset({"drop_newest", "drop_oldest"})
RAW_FAULT_POLICIES = frozenset({"stop_capture", "stop_entries"})


@dataclass(frozen=True)
class RecordingConfig:
    level: RecordingLevel = RecordingLevel.DECISIONS
    explain_trigger: ExplainTrigger = ExplainTrigger.ENTRY_BAND
    # Evaluations kept in memory and written before an actionable event (entry, veto,
    # data fault) so its explanation has preceding context. Bounded.
    explain_context_evaluations: int = 10
    raw_inputs: bool = False
    # On a raw-capture fault: stop capture visibly (default) or also stop new entries
    # cleanly while position management continues (strict qualification runs).
    raw_on_fault: str = "stop_capture"
    directory: Path | None = None  # default: <run output folder>/recording
    queue_max_records: int = 50_000  # per stream
    queue_max_bytes: int = 64 * 1024 * 1024  # per stream (estimated)
    segment_max_bytes: int = 64 * 1024 * 1024  # uncompressed bytes per segment
    segment_max_seconds: float = 300.0
    overflow: str = "drop_newest"
    shutdown_timeout_s: float = 10.0
    retention_days: int = 30  # decisions/explain/raw only, via an explicit prune

    def __post_init__(self) -> None:
        if self.overflow not in OVERFLOW_POLICIES:
            raise ValueError(f"recording.overflow must be one of {sorted(OVERFLOW_POLICIES)}")
        if self.raw_on_fault not in RAW_FAULT_POLICIES:
            raise ValueError(
                f"recording.raw_on_fault must be one of {sorted(RAW_FAULT_POLICIES)}")
        if min(self.queue_max_records, self.queue_max_bytes, self.segment_max_bytes) <= 0:
            raise ValueError("recording queue and segment bounds must be positive")
        if self.segment_max_seconds <= 0 or self.shutdown_timeout_s < 0:
            raise ValueError("recording.segment_max_seconds must be > 0, shutdown >= 0")
        if not 0 <= self.explain_context_evaluations <= 1000:
            raise ValueError("recording.explain_context_evaluations must be in [0, 1000]")
        if self.retention_days < 1:
            raise ValueError("recording.retention_days must be >= 1")

    def identity(self) -> dict:
        return {
            "level": self.level.value,
            "explain_trigger": self.explain_trigger.value,
            "explain_context_evaluations": self.explain_context_evaluations,
            "raw_inputs": self.raw_inputs,
            "raw_on_fault": self.raw_on_fault,
            "overflow": self.overflow,
        }


_KEYS = {
    "level", "explain_trigger", "explain_context_evaluations", "raw_inputs", "raw_on_fault",
    "directory", "queue_max_records", "queue_max_mib", "segment_max_mib",
    "segment_max_seconds", "overflow", "shutdown_timeout_s", "retention_days",
}


def recording_config_from_mapping(raw: Mapping[str, Any] | None) -> RecordingConfig:
    data = dict(raw or {})
    unknown = sorted(set(data) - _KEYS)
    if unknown:
        raise ValueError(f"unknown recording field(s): {unknown}")
    kwargs: dict[str, Any] = {}
    if "level" in data:
        kwargs["level"] = RecordingLevel(str(data["level"]))
    if "explain_trigger" in data:
        kwargs["explain_trigger"] = ExplainTrigger(str(data["explain_trigger"]))
    for key in ("explain_context_evaluations", "queue_max_records", "retention_days"):
        if key in data:
            kwargs[key] = int(data[key])
    if "raw_inputs" in data:
        value = data["raw_inputs"]
        if isinstance(value, str):
            value = value.lower() in ("on", "true", "yes")
        kwargs["raw_inputs"] = bool(value)
    for key in ("raw_on_fault", "overflow"):
        if key in data:
            kwargs[key] = str(data[key])
    if data.get("directory"):
        kwargs["directory"] = Path(str(data["directory"]))
    if "queue_max_mib" in data:
        kwargs["queue_max_bytes"] = int(float(data["queue_max_mib"]) * 1024 * 1024)
    if "segment_max_mib" in data:
        kwargs["segment_max_bytes"] = int(float(data["segment_max_mib"]) * 1024 * 1024)
    for key in ("segment_max_seconds", "shutdown_timeout_s"):
        if key in data:
            kwargs[key] = float(data[key])
    return RecordingConfig(**kwargs)
