"""Decision and explanation records: one builder for live, observation and replay.

The strategy produces its decision row once per evaluation (``reporting_context``); the
host adds the generic part here from objects it already holds at decision time: input
timing (source/receipt/apply times and freshness ages per input, from ``DecisionSnapshot``
and the BookView) and the clock policy state. Nothing is recomputed and no request is
made. Records are fresh dicts of primitives, so a queued record keeps its decision-time
values while indicators keep updating.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from tyrex_pm.recording.config import ExplainTrigger

HOST_FIELDS = ("schema", "mode", "run_id", "window_id", "at_ns", "tau_s", "action", "reason",
               "binance_raw", "model_spot", "chainlink_raw", "sealed_k", "inputs")
_HOST_FIELD_SET = frozenset(HOST_FIELDS)


@dataclass(frozen=True)
class RecordField:
    name: str
    kind: str  # number | text | bool | json
    unit: str | None = None


@dataclass(frozen=True)
class RecordSchema:
    """A strategy's declared decision-record fields (``StrategyPlugin`` hook)."""

    name: str
    version: int
    fields: tuple[RecordField, ...]

    @property
    def schema_id(self) -> str:
        return f"{self.name}_v{self.version}"

    def __post_init__(self) -> None:
        object.__setattr__(self, "_names", frozenset(f.name for f in self.fields))

    def names(self) -> frozenset[str]:
        return self._names  # built once: decision_record runs on every evaluation


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _freshness(assessment: Any) -> dict | None:
    if assessment is None:
        return None
    basis = getattr(assessment, "timestamp_basis", None)
    reason = getattr(assessment, "reason_code", None)
    return {
        "fresh": getattr(assessment, "is_fresh", None),
        "age_ms": getattr(assessment, "age_ms", None),
        "threshold_ms": getattr(assessment, "threshold_ms", None),
        "basis": getattr(basis, "value", basis),
        "reason": getattr(reason, "value", reason),
    }


def _leg(leg: Any) -> dict | None:
    if leg is None:
        return None
    health = getattr(leg, "sync_health", None)
    return {
        "source_ts": _iso(getattr(leg, "source_ts", None)),
        "receive_ts": _iso(getattr(leg, "receive_ts", None)),
        "apply_ts": _iso(getattr(leg, "apply_ts", None)),
        "data_age_ms": getattr(leg, "data_age_ms", None),
        "sync_health": getattr(health, "value", health),
        "book_version": getattr(leg, "book_version", None),
    }


def input_timing(ready: Any, clock: Mapping[str, Any] | None = None) -> dict:
    """Timing of every input the decision used, as available at decision time."""
    snapshot = getattr(ready, "snapshot", None)
    view = getattr(snapshot, "book_view", None) if snapshot is not None else None
    dyn = getattr(ready, "dyn", None)
    return {
        "decision_at": _iso(getattr(snapshot, "observed_at", None)),
        "clock": dict(clock or {}),
        "up_book": _freshness(getattr(snapshot, "yes_freshness", None)),
        "down_book": _freshness(getattr(snapshot, "no_freshness", None)),
        "reference": _freshness(getattr(snapshot, "reference_freshness", None)),
        "up_leg": _leg(getattr(view, "up", None)),
        "down_leg": _leg(getattr(view, "down", None)),
        "book_connection_epoch": getattr(view, "connection_epoch", None),
        "binance_source_ts": _iso(getattr(ready, "binance_source_ts", None)),
        "chainlink_source_ts": _iso(getattr(dyn, "chainlink_source_ts", None)),
    }


def clock_state(time_view: Any) -> dict:
    """The clock policy state behind the decision time (TimeAuthority view)."""
    if time_view is None:
        return {}
    status = getattr(time_view, "sync_status", None)
    return {
        "status": getattr(status, "value", status),
        "offset_ms": getattr(time_view, "estimated_offset_ms", None),
        "uncertainty_ms": getattr(time_view, "uncertainty_ms", None),
        "snapshot_id": getattr(time_view, "clock_snapshot_id", None),
    }


def decision_record(row: Mapping[str, Any], *, mode: str, run_id: str, window_id: Any,
                    schema: RecordSchema | None, inputs: Mapping[str, Any] | None,
                    undeclared: set[str] | None = None) -> dict:
    """Project the evaluated row onto the declared schema plus the host fields.

    Undeclared strategy fields are dropped and their names collected in ``undeclared``
    (reported in the recording status), so a new diagnostic is never silently absorbed.
    """
    out = {"mode": mode, "run_id": run_id, "window_id": window_id}
    if schema is None:
        out.update(row)
    else:
        declared = schema.names()
        for key, value in row.items():
            if key in declared or key in _HOST_FIELD_SET:
                out[key] = value
            elif undeclared is not None:
                undeclared.add(key)
    out["schema"] = None if schema is None else schema.schema_id
    if inputs is not None:
        out["inputs"] = dict(inputs)
    return out


DATA_FAULT_MARKERS = ("NOT_READY", "MISSING", "STALE", "INVALID", "HOST_SKIPPED", "UNKNOWN")
ACTIONABLE_ACTIONS = frozenset({"ENTER", "EXIT", "HOLD", "FLATTEN"})
VETO_REASONS = frozenset({"DISAGREEMENT_VETO", "DISAGREEMENT_INPUT_INVALID"})


def is_actionable(action: Any, reason: Any) -> bool:
    action, reason = str(action or ""), str(reason or "")
    return (action in ACTIONABLE_ACTIONS or reason in VETO_REASONS
            or action == "HOST_SKIPPED"
            or (action in ("WAIT", "BLOCKED") and any(m in reason for m in DATA_FAULT_MARKERS)))


class ExplainSelector:
    """Which evaluations get an explanation record (and bounded preceding context)."""

    def __init__(self, trigger: ExplainTrigger, *, context_evaluations: int,
                 entry_band: tuple[float, float] | None) -> None:
        self.trigger = trigger
        self.entry_band = entry_band
        self._context: deque[dict] = deque(maxlen=max(0, context_evaluations))

    def offer(self, record: dict, *, action: Any, reason: Any, tau_s: Any) -> list[dict]:
        if self.trigger is ExplainTrigger.ALL:
            return [record]
        actionable = is_actionable(action, reason)
        in_band = False
        if self.trigger is ExplainTrigger.ENTRY_BAND and self.entry_band and tau_s is not None:
            low, high = self.entry_band
            try:
                in_band = low <= float(tau_s) <= high
            except (TypeError, ValueError):
                in_band = False
        if not (actionable or in_band):
            if self._context.maxlen:
                self._context.append(record)
            return []
        out: list[dict] = []
        if actionable and self._context:
            out.extend({**r, "context_for_at_ns": record.get("at_ns")} for r in self._context)
        self._context.clear()
        out.append(record)
        return out


def explain_record(*, at_ns: int, window_id: Any, mode: str, run_id: str,
                   indicators: Mapping[str, Any], strategy: Mapping[str, Any] | None,
                   action: Any, reason: Any, tau_s: Any) -> dict:
    return {"mode": mode, "run_id": run_id, "window_id": window_id, "at_ns": at_ns,
            "tau_s": tau_s, "action": getattr(action, "value", action), "reason": reason,
            "indicators": {k: dict(v) for k, v in indicators.items()},
            "strategy": None if strategy is None else dict(strategy)}


def iter_records(directory: Any, stream: str) -> Iterable[dict]:
    """Read verified closed segments in order; an active segment has no manifest."""
    import hashlib
    import io
    import json
    from pathlib import Path

    import zstandard

    for manifest_path in sorted((Path(directory) / stream).glob("part-*.manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = manifest_path.name.removesuffix(".manifest.json") + ".jsonl.zst"
        if manifest.get("file") != name:
            raise ValueError(f"invalid recording segment filename: {manifest_path.name}")
        path = manifest_path.with_name(name)
        count = 0
        with path.open("rb") as raw:
            if hashlib.file_digest(raw, "sha256").hexdigest() != manifest.get("sha256"):
                raise ValueError(f"recording segment checksum mismatch: {name}")
            raw.seek(0)
            reader = io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(raw),
                                      encoding="utf-8")
            for line in reader:
                if line.strip():
                    count += 1
                    yield json.loads(line)
        if count != manifest.get("records"):
            raise ValueError(f"recording segment count mismatch: {name}")
