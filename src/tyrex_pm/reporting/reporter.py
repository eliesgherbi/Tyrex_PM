"""RunReporter / ReportingPort — emit, lanes, health, checkpoints, finalize."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from tyrex_pm.reporting.config import ReportingConfig, default_reporting_config
from tyrex_pm.reporting.contracts import (
    SCHEMA_VERSION,
    DiagnosticsContract,
    EventFamily,
    LineageIds,
    ReportingEvent,
    ReportingHealth,
    ReportingLane,
    RunLifecycle,
    StrategyDiagnosticsBlob,
    default_lane_for_family,
    new_event_id,
    utc_now,
)
from tyrex_pm.reporting.paths import ensure_run_layout
from tyrex_pm.reporting.summary import (
    ClosestCandidate,
    SummaryAccumulators,
    build_run_summary,
    classify_stale_running,
)
from tyrex_pm.reporting.writer import ReportingWriter, atomic_write_json


class ReportingPort(Protocol):
    @property
    def health(self) -> ReportingHealth: ...

    @property
    def allows_new_exposure(self) -> bool: ...

    def emit(self, event: ReportingEvent) -> bool: ...

    def emit_dict(self, **kwargs: Any) -> ReportingEvent: ...

    def persist_critical(self, event: ReportingEvent) -> bool: ...

    def build_pre_mutation_event(self, **kwargs: Any) -> ReportingEvent: ...

    def mark_critical_audit_failure(self, *, reason: str) -> None: ...

    def checkpoint(self) -> None: ...

    def finalize(
        self,
        *,
        terminal_status: str = "COMPLETE",
        terminal_reason: str = "clean_shutdown",
        clean_shutdown: bool = True,
    ) -> Path: ...


@dataclass
class RunReporter:
    """Unified reporter implementing ReportingPort."""

    run_dir: Path
    run_id: str
    mode: str
    strategy_id: str | None = None
    strategy_version: str | None = None
    config: ReportingConfig = field(default_factory=default_reporting_config)
    diagnostics: DiagnosticsContract | None = None
    performance_label: str = "observed_only"
    identity_extra: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    simulation_assumptions: dict[str, Any] = field(default_factory=dict)
    fake_transport: bool = False
    var_root: Path | None = None

    _seq: int = field(default=0, init=False)
    _health: ReportingHealth = field(default=ReportingHealth.HEALTHY, init=False)
    _health_transitions: list[dict[str, Any]] = field(default_factory=list, init=False)
    _lifecycle: RunLifecycle = field(default=RunLifecycle.RUNNING, init=False)
    _accumulators: SummaryAccumulators = field(default_factory=SummaryAccumulators, init=False)
    _writer: ReportingWriter | None = field(default=None, init=False)
    _paths: dict[str, Path] = field(default_factory=dict, init=False)
    _started_at: datetime = field(default_factory=utc_now, init=False)
    _last_checkpoint_at: datetime | None = field(default=None, init=False)
    _events_since_checkpoint: int = field(default=0, init=False)
    _finalized: bool = field(default=False, init=False)
    _market_id: str | None = field(default=None, init=False)
    _window_id: str | None = field(default=None, init=False)
    _attachments: list[dict[str, str]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._accumulators.keep_closest_k = self.config.closest_candidates_per_reason
        self._paths = ensure_run_layout(
            self.run_dir, debug=self.config.debug_host_trace
        )
        self._writer = ReportingWriter(
            audit_path=self._paths["audit_events"],
            analytics_path=self._paths["analytics_events"],
            debug_path=self._paths.get("debug_events"),
            analytics_max_queue=self.config.analytics_max_queue,
            critical_max_queue=self.config.critical_max_queue,
            batch_size=self.config.batch_size,
            on_critical_failure=self._on_critical_failure,
        )
        self._writer.open()
        self._write_manifest(partial=True)
        self.checkpoint()

    @property
    def health(self) -> ReportingHealth:
        return self._health

    @property
    def allows_new_exposure(self) -> bool:
        return self._health is not ReportingHealth.CRITICAL_AUDIT_FAILURE

    @property
    def accumulators(self) -> SummaryAccumulators:
        return self._accumulators

    @property
    def paths(self) -> dict[str, Path]:
        return dict(self._paths)

    def set_market_window(self, *, market_id: str | None, window_id: str | None) -> None:
        self._market_id = market_id
        self._window_id = window_id

    def add_attachment(self, *, name: str, relative_path: str) -> Path:
        attach_dir = self.run_dir / "attachments"
        attach_dir.mkdir(parents=True, exist_ok=True)
        self._attachments.append({"name": name, "path": relative_path})
        self._write_manifest(partial=True)
        return attach_dir

    def emit_dict(
        self,
        *,
        event_family: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        producer: str = "runtime",
        lane: ReportingLane | None = None,
        force_critical: bool = False,
        strategy_id: str | None = None,
        strategy_version: str | None = None,
        market_id: str | None = None,
        window_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        lineage: LineageIds | None = None,
        strategy_diagnostics: StrategyDiagnosticsBlob | None = None,
        event_time: datetime | None = None,
        severity: str = "info",
        skip_detail_persist: bool = False,
    ) -> ReportingEvent:
        self._seq += 1
        resolved_lane = lane or default_lane_for_family(
            event_family, force_critical=force_critical
        )
        event = ReportingEvent(
            schema_version=SCHEMA_VERSION,
            event_id=new_event_id(),
            run_id=self.run_id,
            mode=self.mode,
            sequence=self._seq,
            event_family=event_family,
            event_type=event_type,
            event_time=event_time or utc_now(),
            record_time=utc_now(),
            producer=producer,
            lane=resolved_lane,
            payload=dict(payload or {}),
            strategy_id=strategy_id or self.strategy_id,
            strategy_version=strategy_version or self.strategy_version,
            market_id=market_id or self._market_id,
            window_id=window_id or self._window_id,
            severity=severity,
            correlation_id=correlation_id,
            causation_id=causation_id,
            lineage=lineage or LineageIds(),
            strategy_diagnostics=strategy_diagnostics,
        )
        self.emit(event, skip_detail_persist=skip_detail_persist)
        return event

    def emit(self, event: ReportingEvent, *, skip_detail_persist: bool = False) -> bool:
        # Accumulators first — before any sampling/drop.
        self._accumulators.observe_emit(event)
        if event.event_family == "decision":
            self._maybe_track_closest(event)

        if event.lane is ReportingLane.ANALYTICS:
            if skip_detail_persist or not self._should_persist_analytics(event):
                self._accumulators.note_persisted(event, dropped=True)
                self._maybe_degrade_analytics("sampled_or_disabled")
                self._events_since_checkpoint += 1
                self._maybe_auto_checkpoint()
                return True

        assert self._writer is not None
        result = self._writer.write_event(
            event, durable=(event.lane is ReportingLane.CRITICAL)
        )
        if result.dropped:
            self._accumulators.note_persisted(event, dropped=True)
            self._maybe_degrade_analytics("analytics_backpressure")
            self._events_since_checkpoint += 1
            self._maybe_auto_checkpoint()
            return False
        if not result.accepted and event.lane is ReportingLane.CRITICAL:
            self._set_health(
                ReportingHealth.CRITICAL_AUDIT_FAILURE,
                reason=result.error or "critical_reject",
            )
            return False
        self._accumulators.note_persisted(event, dropped=False)
        self._events_since_checkpoint += 1
        self._maybe_auto_checkpoint()
        return True

    def persist_critical(self, event: ReportingEvent) -> bool:
        """Durable critical ack used by LIVE pre-mutation barrier."""
        self._accumulators.observe_emit(event)
        assert self._writer is not None
        result = self._writer.persist_critical_ack(event)
        if not result.accepted or not result.persisted:
            self._set_health(
                ReportingHealth.CRITICAL_AUDIT_FAILURE,
                reason=result.error or "critical_ack_failed",
            )
            return False
        self._accumulators.note_persisted(event, dropped=False)
        self._events_since_checkpoint += 1
        self.checkpoint()
        return True

    def build_pre_mutation_event(
        self,
        *,
        producer: str,
        payload: Mapping[str, Any],
        lineage: LineageIds | None = None,
        strategy_id: str | None = None,
    ) -> ReportingEvent:
        self._seq += 1
        return ReportingEvent(
            schema_version=SCHEMA_VERSION,
            event_id=new_event_id(),
            run_id=self.run_id,
            mode=self.mode,
            sequence=self._seq,
            event_family=EventFamily.PRE_MUTATION.value,
            event_type="mutation.pre_exposure_increase",
            event_time=utc_now(),
            record_time=utc_now(),
            producer=producer,
            lane=ReportingLane.CRITICAL,
            payload=dict(payload),
            strategy_id=strategy_id or self.strategy_id,
            strategy_version=self.strategy_version,
            market_id=self._market_id,
            window_id=self._window_id,
            severity="critical",
            lineage=lineage or LineageIds(),
        )

    def checkpoint(self) -> None:
        if self._finalized or self._writer is None:
            return
        self._writer.flush()
        self._last_checkpoint_at = utc_now()
        summary = self._build_summary(
            lifecycle=RunLifecycle.RUNNING,
            terminal_status="PARTIAL",
            terminal_reason="checkpoint",
            clean_shutdown=False,
        )
        atomic_write_json(self._paths["run_summary"], summary)
        self._write_manifest(partial=True)
        self._events_since_checkpoint = 0

    def finalize(
        self,
        *,
        terminal_status: str = "COMPLETE",
        terminal_reason: str = "clean_shutdown",
        clean_shutdown: bool = True,
    ) -> Path:
        if self._finalized:
            return self._paths["run_summary"]
        assert self._writer is not None
        self._writer.flush()
        lifecycle = (
            RunLifecycle.COMPLETE
            if terminal_status == "COMPLETE" and clean_shutdown
            else RunLifecycle.ABORTED
            if terminal_status == "ABORTED"
            else RunLifecycle.PARTIAL
        )
        self._lifecycle = lifecycle
        summary = self._build_summary(
            lifecycle=lifecycle,
            terminal_status=terminal_status,
            terminal_reason=terminal_reason,
            clean_shutdown=clean_shutdown,
        )
        atomic_write_json(self._paths["run_summary"], summary)
        self._write_manifest(partial=False, ended=True)
        self._writer.close()
        self._finalized = True
        return self._paths["run_summary"]

    def close(self) -> None:
        if not self._finalized:
            self.finalize(
                terminal_status="PARTIAL",
                terminal_reason="close_without_finalize",
                clean_shutdown=False,
            )

    def force_critical_failure_for_tests(self) -> None:
        assert self._writer is not None
        self._writer.fail_critical_writes = True
        self._set_health(
            ReportingHealth.CRITICAL_AUDIT_FAILURE, reason="test_forced_failure"
        )

    def mark_critical_audit_failure(self, *, reason: str) -> None:
        """Enter CRITICAL_AUDIT_FAILURE after a mutation when audit cannot be persisted."""
        self._on_critical_failure(reason)

    def _should_persist_analytics(self, event: ReportingEvent) -> bool:
        if event.event_family == "decision" and not self.config.per_evaluation:
            # Still persist decisions that emit intents or BLOCKED (force_critical path).
            if event.lane is ReportingLane.CRITICAL:
                return True
            return False
        if event.event_family == "indicator" and not self.config.indicators:
            return False
        if event.event_family == "signal" and not self.config.signals:
            return False
        if event.event_type.startswith("debug.") and not self.config.debug_host_trace:
            return False
        return True

    def _maybe_track_closest(self, event: ReportingEvent) -> None:
        payload = dict(event.payload)
        closest = payload.get("closest_candidate")
        if not isinstance(closest, Mapping):
            return
        if closest.get("reached_edge_evaluation") is not True:
            return
        margin = closest.get("signed_margin")
        if margin is None:
            return
        reason = str(
            payload.get("reason_code")
            or payload.get("primary_reason")
            or closest.get("primary_reason")
            or "UNKNOWN"
        )
        self._accumulators.consider_closest(
            primary_reason=reason,
            candidate=ClosestCandidate(
                evaluation_id=(event.lineage.evaluation_id or closest.get("evaluation_id")),
                market_id=event.market_id,
                window_id=event.window_id,
                selected_leg=closest.get("selected_leg"),
                executable_net_edge=(
                    None
                    if closest.get("executable_net_edge") is None
                    else str(closest.get("executable_net_edge"))
                ),
                required_edge=(
                    None
                    if closest.get("required_edge") is None
                    else str(closest.get("required_edge"))
                ),
                signed_margin=float(margin),
                action=payload.get("action"),
                primary_reason=reason,
                diagnostics=dict(closest.get("diagnostics") or {}),
            ),
        )

    def _maybe_auto_checkpoint(self) -> None:
        if self._events_since_checkpoint >= self.config.checkpoint_every_n_events:
            self.checkpoint()

    def _maybe_degrade_analytics(self, reason: str) -> None:
        if self._health is ReportingHealth.CRITICAL_AUDIT_FAILURE:
            return
        if self._health is not ReportingHealth.DEGRADED_ANALYTICS:
            self._set_health(ReportingHealth.DEGRADED_ANALYTICS, reason=reason)

    def _on_critical_failure(self, reason: str) -> None:
        self._set_health(ReportingHealth.CRITICAL_AUDIT_FAILURE, reason=reason)

    def _set_health(self, health: ReportingHealth, *, reason: str) -> None:
        if health is self._health:
            return
        prev = self._health
        self._health = health
        self._health_transitions.append(
            {
                "from": prev.value,
                "to": health.value,
                "reason": reason,
                "at": utc_now().isoformat().replace("+00:00", "Z"),
            }
        )
        # Health transitions themselves are critical evidence.
        if not self._finalized and self._writer is not None:
            try:
                self._seq += 1
                evt = ReportingEvent(
                    schema_version=SCHEMA_VERSION,
                    event_id=new_event_id(),
                    run_id=self.run_id,
                    mode=self.mode,
                    sequence=self._seq,
                    event_family=EventFamily.REPORTING.value,
                    event_type="reporting.health_transition",
                    event_time=utc_now(),
                    record_time=utc_now(),
                    producer="run_reporter",
                    lane=ReportingLane.CRITICAL,
                    payload={
                        "from": prev.value,
                        "to": health.value,
                        "reason": reason,
                    },
                    strategy_id=self.strategy_id,
                )
                # Avoid recursion through emit health handling: write directly.
                self._writer.write_event(evt, durable=True)
                self._accumulators.observe_emit(evt)
                self._accumulators.note_persisted(evt, dropped=False)
            except Exception:  # noqa: BLE001
                pass

    def _build_summary(
        self,
        *,
        lifecycle: RunLifecycle,
        terminal_status: str,
        terminal_reason: str,
        clean_shutdown: bool,
    ) -> dict[str, Any]:
        ended = utc_now() if lifecycle is not RunLifecycle.RUNNING else None
        identity = {
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "mode": self.mode,
            "market_id": self._market_id,
            "window_id": self._window_id,
            "started_at": self._started_at.isoformat().replace("+00:00", "Z"),
            "ended_at": None
            if ended is None
            else ended.isoformat().replace("+00:00", "Z"),
            "fake_transport": self.fake_transport,
            **self.identity_extra,
        }
        status = {
            "lifecycle": lifecycle.value,
            "terminal_status": terminal_status,
            "terminal_reason": terminal_reason,
            "clean_shutdown": clean_shutdown,
        }
        artifacts = {
            "manifest": "manifest.json",
            "run_summary": "run_summary.json",
            "audit_events": "audit_events.jsonl",
            "analytics_events": "analytics_events.jsonl",
            "attachments": list(self._attachments),
        }
        if "debug_events" in self._paths:
            artifacts["diagnostics"] = "diagnostics/debug_events.jsonl"
        return build_run_summary(
            accumulators=self._accumulators,
            identity=identity,
            status=status,
            configuration=dict(self.configuration),
            performance_label=self.performance_label,
            reporting_health=self._health,
            health_transitions=list(self._health_transitions),
            last_durable_sequence=(
                0 if self._writer is None else self._writer.last_durable_sequence
            ),
            checkpoint_time=self._last_checkpoint_at,
            artifacts=artifacts,
            simulation_assumptions=self.simulation_assumptions,
        )

    def _write_manifest(self, *, partial: bool, ended: bool = False) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "mode": self.mode,
            "run_kind": "ops" if self.mode == "_ops" else "strategy",
            "lifecycle": self._lifecycle.value if ended else RunLifecycle.RUNNING.value,
            "partial": partial and not ended,
            "reporting_health": self._health.value,
            "started_at": self._started_at.isoformat().replace("+00:00", "Z"),
            "updated_at": utc_now().isoformat().replace("+00:00", "Z"),
            "last_checkpoint_at": None
            if self._last_checkpoint_at is None
            else self._last_checkpoint_at.isoformat().replace("+00:00", "Z"),
            "last_durable_sequence": (
                0 if self._writer is None else self._writer.last_durable_sequence
            ),
            "performance_label": self.performance_label,
            "fake_transport": self.fake_transport,
            "reporting_profile": self.config.profile,
            "configuration": dict(self.configuration),
            "attachments": list(self._attachments),
            "artifacts": {
                "run_summary": "run_summary.json",
                "audit_events": "audit_events.jsonl",
                "analytics_events": "analytics_events.jsonl",
            },
            **self.identity_extra,
        }
        atomic_write_json(self._paths["manifest"], payload)


def open_run_reporter(
    *,
    run_dir: Path,
    run_id: str | None = None,
    mode: str,
    strategy_id: str | None = None,
    strategy_version: str | None = None,
    config: ReportingConfig | None = None,
    diagnostics: DiagnosticsContract | None = None,
    performance_label: str | None = None,
    configuration: Mapping[str, Any] | None = None,
    simulation_assumptions: Mapping[str, Any] | None = None,
    fake_transport: bool = False,
    identity_extra: Mapping[str, Any] | None = None,
) -> RunReporter:
    label = performance_label
    if label is None:
        if mode == "shadow":
            label = "simulated"
        elif mode == "live":
            label = "real"
        else:
            label = "observed_only"
    return RunReporter(
        run_dir=Path(run_dir),
        run_id=run_id or str(uuid4()),
        mode=mode,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        config=config or default_reporting_config(),
        diagnostics=diagnostics,
        performance_label=label,
        configuration=dict(configuration or {}),
        simulation_assumptions=dict(simulation_assumptions or {}),
        fake_transport=fake_transport,
        identity_extra=dict(identity_extra or {}),
    )


def inspect_run_manifest(path: Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    stale = classify_stale_running(data)
    data["stale_classification"] = stale
    return data
