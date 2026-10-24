"""Strategy evaluation (decision context, driver call, intents) and decision recording."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from tyrex_pm.indicators.graph import FeatureBundle
from tyrex_pm.runtime.decision_records import decision_context
from tyrex_pm.runtime.host.shared import (
    RECORDING_STATUS_INTERVAL_S,
    _compact_decision_evidence,
    _lifecycle_view,
    _recording_status_event,
)
from tyrex_pm.runtime.market_runtime import ZGapMarketRuntime
from tyrex_pm.runtime.run_config import (
    max_clock_uncertainty_ms,
)
from tyrex_pm.strategies.triggers import EvaluationTrigger


class DecisionMixin:
    """Strategy evaluation (decision context, driver call, intents) and decision recording.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def on_evaluation(self, market_runtime: ZGapMarketRuntime) -> list[dict[str, Any]]:
        self._market_runtime_ref = market_runtime
        ready = market_runtime.prepare_aligned_eval()
        if not ready.ok or ready.snapshot is None or ready.binding is None:
            self.capabilities.model_ready = False
            self.run_recorder.record(
                "EVALUATION_SKIPPED",
                {"reasons": list(ready.skip_reasons)},
            )
            if self.decision_recording is not None:
                self._record_skipped_decision(ready, market_runtime)
            self._record_readiness_if_changed()
            return [{"kind": "skipped", "reasons": list(ready.skip_reasons)}]
        state = self.lifecycle.state
        exposed = bool(state is not None and state.has_exposure)
        market = ready.session.market if ready.session is not None else ready.snapshot.market
        instrument = self.selected_instrument
        life = _lifecycle_view(
            state, instrument_id=None if instrument is None else instrument.instrument_id
        )
        caps = self.capabilities.snapshot(exposed=exposed)
        context = decision_context(
            ready,
            monotonic_ns=time.monotonic_ns(),
            run_id=self.config.run_name,
            target_notional=self.config.risk.target_notional,
            lifecycle=life,
            position_quantity=Decimal("0") if state is None else state.confirmed_position_shares,
            position_cost_total=self._position_cost(state),
            entry_allowed=caps.entry_executable and not exposed and state is None,
            entry_block_reason=None if caps.entry_executable else ",".join(caps.blockers),
            exit_allowed=caps.exit_executable,
            exit_block_reason=None if caps.exit_executable else ",".join(caps.blockers),
            unknown_inventory=not self.capabilities.prior_scope_clear,
            kill_switch_active=self.capabilities.kill_switch_active,
            last_exit_reason=(
                None
                if ready.session is None
                else self._last_exit_reason.get(ready.session.window_id)
            ),
        )
        graph = getattr(market_runtime, "indicator_graph", None)
        features = graph.snapshot() if graph is not None else FeatureBundle()
        capture = getattr(self.decision_recording, "raw", None)
        if capture is not None:
            from tyrex_pm.recording.trading import context_input

            window_id = self.active_market.window_id
            if window_id not in self._capture_seeded_windows:
                driver = self._driver_for(window_id)
                capture.event("strategy_feedback", "feedback", {
                    "window_id": window_id, "kind": "seed",
                    "payload": {} if driver is None else driver.persistence_slice()})
                self._capture_seeded_windows[window_id] = True
                if len(self._capture_seeded_windows) > 64:
                    self._capture_seeded_windows.pop(next(iter(self._capture_seeded_windows)))
            self._trading_evaluation_seq += 1
            session_id = self.lifecycle.session_id
            account = self.account_state_authority.current(
                market.condition_id or market.market_id.value)
            account_ref = None
            if account is not None:
                account_ref = (f"{account.market_id}:{account.observed_monotonic_s}:"
                               f"{account.status.value}:{account.read_attempt}")
                if account_ref not in self._captured_account_refs:
                    capture.event("account_snapshot", "account", {
                        "account_ref": account_ref, "snapshot": account})
                    self._captured_account_refs[account_ref] = True
                    if len(self._captured_account_refs) > 64:
                        self._captured_account_refs.pop(next(iter(self._captured_account_refs)))
            capture.event("trading_context", "context", {
                "evaluation_id": self._trading_evaluation_seq,
                "window_id": self.active_market.window_id,
                "context": context_input(context),
                "monotonic_ns": self.time_authority.view().monotonic_ns,
                "session_id": session_id,
                "account_ref": account_ref,
                "applied_event_id": self.coordinator.applied_event_ids.get(session_id),
                "ledger_sequence": (None if self.settlement_hooks is None
                                    else self.settlement_hooks.ledger._seq)})
        result = ready.binding.evaluate(
            market_snapshot=ready.snapshot,
            causation_id=ready.snapshot.causation_id,
            correlation_id=ready.snapshot.correlation_id,
            trigger=EvaluationTrigger.FEED,
            context=context,
            features=features,
        )
        if capture is not None:
            from tyrex_pm.runtime.decision_records import evaluated_record

            at_ns, end_ns = self._recording_times(ready, ready.session)
            capture.event("trading_decision", "decision", {
                "evaluation_id": self._trading_evaluation_seq,
                "window_id": self.active_market.window_id,
                "decision": evaluated_record(ready, result, at_ns=at_ns, window_end_ns=end_ns),
                "intents": [{"id": i.intent_id.value, "kind": type(i).__name__,
                             "reason": i.reason_code} for i in result.intents]})
        self.capabilities.model_ready = result.eligibility.model_ready
        decision_evidence = dict(result.decision.evidence)
        if self.decision_recording is not None:
            self._record_evaluated_decision(ready, result, market_runtime)
            # The full row lives in the decision recording; keep the journal compact.
            decision_evidence = _compact_decision_evidence(decision_evidence)
        strategy_blockers = result.eligibility.blockers
        strategy_inputs_eligible = result.eligibility.strategy_inputs_eligible
        decision_record = {
            "decided_at": result.decision.decided_at.isoformat(),
            "action": result.decision.action.value,
            "reason_code": result.decision.reason_code,
            "market_id": market.market_id.value,
            "window_id": ready.session.window_id if ready.session is not None else None,
            "intent_count": len(result.intents),
            "capabilities": self.capabilities.snapshot(exposed=exposed).__dict__,
            "strategy_inputs_eligible": strategy_inputs_eligible,
            "strategy_blockers": list(strategy_blockers),
            "decision_evidence": decision_evidence,
            "calibration": dict(result.reporting_context.get("calibration") or {}),
        }
        self.decisions.append(decision_record)
        self.run_recorder.record("STRATEGY_DECISION", decision_record)
        self._record_readiness_if_changed()
        for intent in result.intents:
            key = intent.semantic_key()
            if key not in self.seen_intents:
                self.seen_intents.add(key)
                self._enqueue_intent(intent)
                self.run_recorder.record(
                    "INTENT_EMITTED",
                    {
                        "intent_id": intent.intent_id.value,
                        "intent_type": type(intent).__name__,
                        "reason_code": intent.reason_code,
                        "market_id": intent.market_id.value,
                        "instrument_id": intent.instrument_id.value,
                        "created_at": intent.created_at,
                    },
                )
        return [{"kind": "evaluated", **decision_record}]

    def _capture_rest_book(self, book, metadata) -> None:
        capture = getattr(self.decision_recording, "raw", None)
        if capture is not None:
            capture.message("applied_rest_book", {"book": book, **metadata},
                            connection=f"{metadata['binding_id']}:{metadata['connection_epoch']}",
                            transport="collector_event", body_kind="host_event")

    def _open_recording(self) -> None:
        settings = getattr(self.config, "recording", None)
        if settings is None:
            return
        from tyrex_pm.recording.host import open_decision_recording
        from tyrex_pm.recording.raw import _jsonable
        from tyrex_pm.strategies.registry import get_strategy_plugin

        try:
            plugin = get_strategy_plugin(self.config.strategy_kind)
            schema_hook = getattr(plugin, "decision_record_schema", None)
            schema = None if schema_hook is None else schema_hook(self.config.strategy)
            try:
                band = tuple(float(v) for v in plugin.entry_tau_bounds(self.config.strategy))
            except Exception:  # noqa: BLE001 - the explain band is optional
                band = None
            repro = self.reproducibility or {}
            self.decision_recording = open_decision_recording(
                settings,
                output_directory=self.output_directory,
                mode="paper" if getattr(self.config, "venue", "live") == "paper" else "live",
                run_id=self.run_instance_id,
                schema=schema,
                entry_band=band,
                manifest={
                    "configured_run_name": self.config.run_name,
                    "strategy_kind": self.config.strategy_kind,
                    "strategy_parameters": _jsonable(self.config.strategy),
                    "book_reconstruction": "venue_top_v1",
                    "trading_capture_version": "recorded_execution_v1",
                    "risk_policy": _jsonable(self.config.risk),
                    "account_policy": _jsonable(self.config.account),
                    "settlement_limits": (None if self.config.settlement is None else
                                          _jsonable(self.config.settlement.run_limits)),
                    "reproducibility": repro,
                    "clock_policy": {
                        "max_uncertainty_ms": max_clock_uncertainty_ms(self.config),
                    },
                    "feature_policies": [
                        getattr(spec, "instance_id", str(spec))
                        for spec in self._required_feature_specs
                    ],
                    "evaluation_interval_s": self.config.market.evaluation_interval_s,
                },
            )
        except Exception as exc:  # noqa: BLE001 - recording never blocks trading
            self.decision_recording = None
            self.recording_status = {"failed": True,
                                     "error": f"{type(exc).__name__}: {exc}"}
            self.run_recorder.record("RECORDING_FAILED", {"stage": "open",
                                                          **self.recording_status})
            return
        if self.decision_recording is not None:
            self.run_recorder.record("RECORDING_STARTED", {
                "directory": str(self.decision_recording.recorder.directory),
                "level": settings.level.value,
                "explain_trigger": settings.explain_trigger.value,
            })

    def _recording_time_view(self) -> Any:
        authority = self.time_authority
        try:
            return None if authority is None else authority.view()
        except Exception:  # noqa: BLE001
            return None

    def _record_skipped_decision(self, ready: Any, market_runtime: Any) -> None:
        recording = self.decision_recording
        if recording is None:
            return
        from tyrex_pm.runtime.decision_records import skipped_record

        session = ready.session or getattr(market_runtime, "active", None)
        at_ns, end_ns = self._recording_times(ready, session)
        capture = getattr(recording, "raw", None)
        if capture is not None:
            self._trading_evaluation_seq += 1
            capture.event("trading_skipped", "skipped", {
                "evaluation_id": self._trading_evaluation_seq,
                "window_id": None if session is None else session.window_id,
                "decision": skipped_record(ready, at_ns=at_ns, window_end_ns=end_ns)})
        recording.on_skipped(
            ready, skipped_record(ready, at_ns=at_ns, window_end_ns=end_ns),
            window_id=None if session is None else session.window_id,
            time_view=self._recording_time_view())

    def _record_evaluated_decision(self, ready: Any, result: Any, market_runtime: Any) -> None:
        from tyrex_pm.runtime.decision_records import evaluated_record

        session = ready.session
        at_ns, end_ns = self._recording_times(ready, session)
        self.decision_recording.on_evaluated(
            ready, evaluated_record(ready, result, at_ns=at_ns, window_end_ns=end_ns),
            window_id=None if session is None else session.window_id,
            time_view=self._recording_time_view(),
            graph=getattr(market_runtime, "indicator_graph", None))

    def _recording_times(self, ready: Any, session: Any) -> tuple[int, int | None]:
        snapshot = getattr(ready, "snapshot", None)
        if snapshot is not None:
            at = snapshot.observed_at
        elif self.time_authority is not None:
            at = self.time_authority.now_corrected_utc()
        else:
            at = datetime.now(timezone.utc)
        end = None if session is None else getattr(session.market, "event_end", None)
        return (int(at.timestamp() * 1e9),
                None if end is None else int(end.timestamp() * 1e9))

    def _recording_heartbeat(self, *, force: bool = False) -> None:
        """RECORDING_STATUS every 60 s; RECORDING_FAILED once when a stream fails."""
        recording = self.decision_recording
        if recording is None:
            return
        now = time.monotonic()
        if not force and now - self._recording_status_mono < RECORDING_STATUS_INTERVAL_S:
            return
        self._recording_status_mono = now
        status = recording.status()
        status["run_evidence"] = self.run_recorder.status()
        self.recording_status = status
        self.run_recorder.record("RECORDING_STATUS", _recording_status_event(status))
        if status.get("failed") and not self._recording_failed_reported:
            self._recording_failed_reported = True
            self.run_recorder.record("RECORDING_FAILED", {
                "stage": "write",
                "writer_error": status.get("writer_error"),
                "streams": {name: snap.get("last_error")
                            for name, snap in status.get("streams", {}).items()
                            if snap.get("state") == "failed"},
                "effect": "decision recording stopped; trading continues",
            })

    def _check_raw_capture(self) -> None:
        """Apply ``recording.raw_on_fault`` once when raw capture becomes incomplete.

        ``stop_capture`` (default): capture stops visibly, trading continues.
        ``stop_entries`` (strict qualification runs): also block new entries; exits,
        holds and position management are unaffected (entry-only blocker).
        """
        recording = self.decision_recording
        if self._raw_fault_handled or recording is None or recording.raw is None:
            return
        reason = recording.raw_fault()
        if reason is None:
            return
        self._raw_fault_handled = True
        recording.stop_raw(reason)
        policy = self.config.recording.raw_on_fault
        if policy == "stop_entries":
            self.capabilities.set_blocker("RAW_CAPTURE_FAILED", reason)
        self.run_recorder.record("RAW_CAPTURE_FAILED", {
            "reason": reason, "policy": policy,
            "effect": "raw capture stopped; new entries blocked, positions managed"
            if policy == "stop_entries"
            else "raw capture stopped; trading continues",
        })
        self._record_readiness_if_changed()

    def _close_recording(self) -> None:
        recording = self.decision_recording
        if recording is None:
            return
        try:
            if recording.raw is not None:
                from tyrex_pm.recording.trading import export_evidence

                try:
                    export_evidence(self.config, self.output_directory, self.run_instance_id)
                except Exception as exc:  # noqa: BLE001 - still drain optional recording
                    self.run_recorder.record("EXECUTION_EXPORT_FAILED", {"error": str(exc)})
            status = recording.close()
        except Exception as exc:  # noqa: BLE001
            status = {"failed": True, "error": f"{type(exc).__name__}: {exc}"}
        status["run_evidence"] = self.run_recorder.status()
        self.recording_status = status
        self.run_recorder.record("RECORDING_CLOSED", _recording_status_event(status))

    def _capture_feedback(self, window_id, kind, payload) -> None:
        capture = getattr(self.decision_recording, "raw", None)
        if capture is not None:
            from tyrex_pm.recording.trading import feedback_input

            capture.event("strategy_feedback", "feedback", {
                "window_id": window_id, "kind": kind,
                "payload": payload if isinstance(payload, dict) else feedback_input(payload)})
