"""Host-side decision recording shared by live trading, observation and replay hosts.

The host calls ``on_skipped`` / ``on_evaluated`` once per evaluation with objects it
already holds. Everything copied here is a decision-time snapshot of primitives (the
strategy row, input timing, the clock view and, when selected, indicator ``explain()``),
so the queued record does not change while indicators keep updating. The cost of this
call on the trading loop is measured and reported (``capture_cost_us``).
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any, Mapping

from tyrex_pm.recording.config import RecordingConfig, RecordingLevel
from tyrex_pm.recording.recorder import Recorder
from tyrex_pm.recording.records import (
    ExplainSelector,
    RecordSchema,
    clock_state,
    decision_record,
    explain_record,
    input_timing,
)

_COST_SAMPLES = 4096


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)


class DecisionRecording:
    def __init__(self, recorder: Recorder, *, mode: str, run_id: str,
                 schema: RecordSchema | None, level: RecordingLevel,
                 selector: ExplainSelector | None) -> None:
        self.recorder = recorder
        self.mode = mode
        self.run_id = run_id
        self.schema = schema
        self.level = level
        self.selector = selector
        self.undeclared: set[str] = set()
        self.capture_errors = 0
        self.last_capture_error: str | None = None
        self._cost_us: deque[float] = deque(maxlen=_COST_SAMPLES)

    # -- trading loop ------------------------------------------------------------------
    def on_skipped(self, ready: Any, row: Mapping[str, Any], *, window_id: Any,
                   time_view: Any = None) -> None:
        self._capture(ready, row, window_id=window_id, time_view=time_view, graph=None,
                      strategy_explain=None)

    def on_evaluated(self, ready: Any, row: Mapping[str, Any], *, window_id: Any,
                     time_view: Any = None, graph: Any = None,
                     strategy_explain: Mapping[str, Any] | None = None) -> None:
        self._capture(ready, row, window_id=window_id, time_view=time_view, graph=graph,
                      strategy_explain=strategy_explain)

    def _capture(self, ready: Any, row: Mapping[str, Any], *, window_id: Any,
                 time_view: Any, graph: Any, strategy_explain: Any) -> None:
        started = time.perf_counter_ns()
        try:
            record = decision_record(
                row, mode=self.mode, run_id=self.run_id, window_id=window_id,
                schema=self.schema, inputs=input_timing(ready, clock_state(time_view)),
                undeclared=self.undeclared)
            at_ns = record.get("at_ns")
            self.recorder.capture("decisions", record,
                                  capture_ns=at_ns if isinstance(at_ns, int) else None)
            if self.selector is not None:
                action, reason, tau_s = record.get("action"), record.get("reason"), \
                    record.get("tau_s")
                explain = explain_record(
                    at_ns=at_ns, window_id=window_id, mode=self.mode, run_id=self.run_id,
                    indicators={} if graph is None else graph.explain(),
                    strategy=strategy_explain, action=action, reason=reason, tau_s=tau_s)
                for item in self.selector.offer(explain, action=action, reason=reason,
                                                tau_s=tau_s):
                    self.recorder.capture("explain", item)
        except Exception as exc:  # noqa: BLE001 - recording never stops the strategy
            self.capture_errors += 1
            self.last_capture_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._cost_us.append((time.perf_counter_ns() - started) / 1000.0)

    # -- status ------------------------------------------------------------------------
    def status(self) -> dict:
        costs = list(self._cost_us)
        status = self.recorder.status()
        status.update({
            "mode": self.mode,
            "level": self.level.value,
            "schema": None if self.schema is None else self.schema.schema_id,
            "undeclared_fields": sorted(self.undeclared),
            "capture_errors": self.capture_errors,
            "last_capture_error": self.last_capture_error,
            "capture_cost_us": {"p50": _percentile(costs, 0.5),
                                "p99": _percentile(costs, 0.99),
                                "max": None if not costs else round(max(costs), 2),
                                "samples": len(costs)},
        })
        return status

    def close(self) -> dict:
        closed = self.recorder.close()
        merged = self.status()
        merged.update({k: v for k, v in closed.items() if k not in merged or k == "streams"})
        merged["writer_finished"] = closed.get("writer_finished")
        return merged


def open_decision_recording(config: RecordingConfig, *, output_directory: Path, mode: str,
                            run_id: str, schema: RecordSchema | None,
                            entry_band: tuple[float, float] | None,
                            manifest: Mapping[str, Any],
                            record_decisions: bool = True) -> DecisionRecording | None:
    """Start recording for ``config.level``; None when there is nothing to record.

    ``record_decisions=False`` is for hosts that already write their own decision rows
    (observation): only the explain stream is opened here, at level ``explain``.
    """
    if not config.level.includes(RecordingLevel.DECISIONS):
        return None
    if not record_decisions and not config.level.includes(RecordingLevel.EXPLAIN):
        return None
    streams: dict[str, str | None] = {}
    if record_decisions:
        streams["decisions"] = None if schema is None else schema.schema_id
    selector = None
    if config.level.includes(RecordingLevel.EXPLAIN):
        streams["explain"] = "indicator_explain_v1"
        selector = ExplainSelector(config.explain_trigger,
                                   context_evaluations=config.explain_context_evaluations,
                                   entry_band=entry_band)
    directory = config.directory or (Path(output_directory) / "recording")
    recorder = Recorder(directory, config, streams=streams, manifest={
        "mode": mode, "run_id": run_id, "level": config.level.value,
        "schemas": dict(streams), "entry_band": entry_band, **dict(manifest)})
    recorder.start()
    return DecisionRecording(recorder, mode=mode, run_id=run_id, schema=schema,
                             level=config.level, selector=selector)
