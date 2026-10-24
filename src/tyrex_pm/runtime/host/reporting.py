"""Run report: execution timeline and report files."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from tyrex_pm.execution.evidence import (
    DispatchAuthorized,
    OrderPreDispatchFailed,
    OrderPrepared,
    OrderRequested,
    SubmissionAttempted,
    SubmissionFailed,
    SubmissionResponseObserved,
)
from tyrex_pm.execution.session_state import (
    ExecutionSessionState,
)
from tyrex_pm.persistence.run_evidence_journal import (
    RunEvidenceRecord,
)
from tyrex_pm.reporting.run_report import (
    RunReportInput,
    build_run_report,
    write_emergency_report,
    write_run_report,
)
from tyrex_pm.runtime.host.shared import (
    TradingRunResult,
    _merge_prepare_timings,
)


class ReportingMixin:
    """Run report: execution timeline and report files.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def _max_duration_s(self) -> float | None:
        market = self.config.market
        if getattr(market, "run_mode", "single_window") == "continuous":
            # Until stopped, unless an explicit bounded soak length was configured.
            return market.maximum_duration_s if market.maximum_duration_explicit else None
        return market.maximum_duration_s

    def _build_execution_timeline(
        self, state: ExecutionSessionState | None
    ) -> list[dict[str, Any]]:
        if state is None or state.session_id is None:
            return []
        rows: dict[str, dict[str, Any]] = {}
        for order_id, order in state.orders.items():
            candidate_raw = order.spec.metadata.get("candidate_at")
            candidate_mono_raw = order.spec.metadata.get("candidate_monotonic_ns")
            candidate = None
            if candidate_raw:
                try:
                    candidate = datetime.fromisoformat(str(candidate_raw).replace("Z", "+00:00"))
                except ValueError:
                    candidate = None
            rows[order_id] = {
                "order_id": order_id,
                "role": order.role.value,
                "candidate_at": None if candidate is None else candidate.isoformat(),
                "requested_at": None,
                "prepared_at": None,
                "pre_dispatch_failed_at": None,
                "dispatch_authorized_at": None,
                "http_post_started_at": None,
                "http_post_completed_at": None,
                "submission_outcome": None,
                "pre_dispatch_failure": None,
                "requested_protection_price": (
                    None
                    if order.requested_protection_price is None
                    else str(order.requested_protection_price)
                ),
                "effective_protection_price": (
                    None
                    if order.effective_protection_price is None
                    else str(order.effective_protection_price)
                ),
                "tick_size": None if order.tick_size is None else str(order.tick_size),
                "_candidate_mono": (
                    None if candidate_mono_raw is None else int(candidate_mono_raw)
                ),
            }
        for event in self.coordinator.journal.load(state.session_id):
            if isinstance(
                event,
                (
                    OrderRequested,
                    OrderPrepared,
                    OrderPreDispatchFailed,
                    DispatchAuthorized,
                    SubmissionAttempted,
                    SubmissionResponseObserved,
                    SubmissionFailed,
                ),
            ):
                event_order_id = (
                    event.order.order_id if isinstance(event, OrderRequested) else event.order_id
                )
                row = rows.get(event_order_id)
                if row is None:
                    continue
                if isinstance(event, OrderRequested):
                    row["requested_at"] = event.observed_at.isoformat()
                    row["_requested_mono"] = event.observed_monotonic_ns
                elif isinstance(event, OrderPrepared):
                    row["prepared_at"] = event.observed_at.isoformat()
                    row["_prepared_mono"] = event.observed_monotonic_ns
                    _merge_prepare_timings(row, event)
                elif isinstance(event, OrderPreDispatchFailed):
                    row["pre_dispatch_failed_at"] = event.observed_at.isoformat()
                    row["_pre_dispatch_failed_mono"] = event.observed_monotonic_ns
                    row["submission_outcome"] = "pre_dispatch_failed"
                    row["pre_dispatch_failure"] = {
                        "stage": event.stage.value,
                        "error_class": event.error_class,
                        "error_code": event.error_code,
                        "message": event.message,
                    }
                    _merge_prepare_timings(row, event)
                elif isinstance(event, DispatchAuthorized):
                    row["dispatch_authorized_at"] = event.observed_at.isoformat()
                    row["_authorized_mono"] = event.observed_monotonic_ns
                elif isinstance(event, SubmissionAttempted):
                    row["http_post_started_at"] = event.observed_at.isoformat()
                    row["_post_started_mono"] = event.observed_monotonic_ns
                else:
                    row["http_post_completed_at"] = event.observed_at.isoformat()
                    row["_post_completed_mono"] = event.observed_monotonic_ns
                    row["submission_outcome"] = (
                        "response"
                        if isinstance(event, SubmissionResponseObserved)
                        else "ambiguous_failure"
                    )
        result: list[dict[str, Any]] = []
        for row in rows.values():
            pairs = {
                "candidate_to_request_ms": ("_candidate_mono", "_requested_mono"),
                "request_to_prepared_ms": ("_requested_mono", "_prepared_mono"),
                "request_to_pre_dispatch_failure_ms": (
                    "_requested_mono",
                    "_pre_dispatch_failed_mono",
                ),
                "request_to_authorized_ms": ("_requested_mono", "_authorized_mono"),
                "authorized_to_post_ms": ("_authorized_mono", "_post_started_mono"),
                "post_round_trip_ms": ("_post_started_mono", "_post_completed_mono"),
                "candidate_to_post_ms": ("_candidate_mono", "_post_started_mono"),
                "candidate_to_pre_dispatch_failure_ms": (
                    "_candidate_mono",
                    "_pre_dispatch_failed_mono",
                ),
            }
            for label, (start_key, end_key) in pairs.items():
                start, end = row.get(start_key), row.get(end_key)
                row[label] = (
                    None
                    if start is None or end is None
                    else round(max(0, end - start) / 1_000_000, 3)
                )
            result.append({key: value for key, value in row.items() if not key.startswith("_")})
        return result

    def _write_report(
        self,
        *,
        state: ExecutionSessionState | None,
        mutation_attempts: int | None,
        run_evidence: tuple[RunEvidenceRecord, ...],
        fatal_error: dict[str, Any] | None,
        reporting_failures: list[str],
    ) -> TradingRunResult:
        path = self.output_directory / "run_summary.json"
        try:
            capabilities = self.readiness_dict()
        except Exception as exc:
            capabilities = {}
            reporting_failures.append(f"capability_projection:{type(exc).__name__}:{exc}")
        try:
            market_data = None if self.market_summary is None else self.market_summary.to_dict()
        except Exception as exc:
            market_data = None
            reporting_failures.append(f"market_projection:{type(exc).__name__}:{exc}")
        venue = getattr(self.config, "venue", None)
        armed = self.capabilities.live_requested
        source = RunReportInput(
            run_instance_id=self.run_instance_id,
            configured_run_name=self.config.run_name,
            live_requested=armed,
            venue=venue,
            real_orders_authorized=venue == "live" and armed,
            state=state,
            mutation_attempts=mutation_attempts,
            process_mutation_attempts=self._process_mutation_attempts,
            strategy_started=self._strategy_started,
            execution_timeline=self.execution_timeline,
            capabilities=capabilities,
            market_data=market_data,
            run_evidence=run_evidence,
            runtime_errors=self.runtime_errors,
            fatal_error=fatal_error,
            reporting_failures=reporting_failures,
            prior_session_risk=self.prior_session_risk,
            reproducibility=self.reproducibility,
            startup_outcome=self.startup_outcome,
            startup_reconciliation=self.startup_report,
            recovery_sessions=tuple(self.recovery_sessions),
            run_sessions=self._run_session_states,
            committed_session_ids=frozenset(
                getattr(self.lifecycle, "detached_sessions", ())
            ),
            recording=self.recording_status,
        )
        try:
            payload = build_run_report(source)
            write_run_report(path, payload)
        except Exception as exc:  # primitive-only final fallback
            payload = write_emergency_report(
                path,
                run_instance_id=self.run_instance_id,
                configured_run_name=self.config.run_name,
                live_requested=armed,
                mutation_attempts=mutation_attempts,
                fatal_error=fatal_error,
                reporting_error=exc,
                reproducibility=self.reproducibility,
                venue=venue,
                real_orders_authorized=venue == "live" and armed,
            )
        return TradingRunResult(
            outcome=str(payload["outcome"]),
            ok=bool(payload["ok"]),
            report_path=path,
            mutation_attempts=mutation_attempts,
            state=state,
        )
