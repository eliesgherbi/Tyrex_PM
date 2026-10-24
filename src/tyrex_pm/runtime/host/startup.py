"""Startup assessment, validity monitoring, rechecks and operator recovery."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.intents import (
    FlattenIntent,
    new_intent_id,
)
from tyrex_pm.runtime.host.shared import (
    _recovery_observation,
    _try_execution_epoch,
    _tty_decision_reader,
)


class StartupMixin:
    """Startup assessment, validity monitoring, rechecks and operator recovery.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def note_assessment_validity(self) -> None:
        """Local validity only. Strategy evaluation does not call the account."""
        deadline = self._assessment_valid_until
        if deadline is None or self.time_authority is None:
            return
        now = self.time_authority.now_corrected_utc()
        if now >= deadline:
            self.capabilities.set_blocker(
                "STARTUP_ASSESSMENT_EXPIRED",
                "startup evidence is older than its validity window",
            )

    def apply_startup_recheck(self, ready: bool) -> None:
        if ready:
            self.capabilities.clear_blocker("STARTUP_ASSESSMENT_EXPIRED")
            self.capabilities.clear_blocker("STARTUP_CHECK_PENDING")
            if self.time_authority is not None:
                from datetime import timedelta

                self._assessment_valid_until = (
                    self.time_authority.now_corrected_utc()
                    + timedelta(seconds=self._startup_validity_window_s())
                )
        else:
            self.capabilities.set_blocker(
                "STARTUP_CHECK_PENDING",
                "startup recheck is not ready",
            )

    def startup_buy_block(self) -> str | None:
        """Local freshness check used only at final BUY authorization."""
        if self._startup_invalidated_reason:
            return self._startup_invalidated_reason
        if self._assessment_generation not in {None, self.run_instance_id}:
            return "STARTUP_ASSESSMENT_EXPIRED"
        until = self._assessment_valid_until
        if until is None:
            return "STARTUP_ASSESSMENT_EXPIRED"
        now = (
            datetime.now(timezone.utc)
            if self.time_authority is None
            else self.time_authority.now_corrected_utc()
        )
        if now >= until:
            return "STARTUP_ASSESSMENT_EXPIRED"
        return None

    def note_startup_stream_gap(self) -> None:
        self._startup_invalidated_reason = "STARTUP_STREAM_GAP"
        self._assessment_valid_until = None
        self.capabilities.set_blocker(
            "STARTUP_ASSESSMENT_EXPIRED",
            "the book stream gap invalidated startup readiness",
        )

    def _mark_startup_fresh(self) -> None:
        from datetime import timedelta

        self._startup_invalidated_reason = None
        self._assessment_generation = self.run_instance_id
        self.capabilities.clear_blocker("STARTUP_ASSESSMENT_EXPIRED")
        self.capabilities.clear_blocker("STARTUP_CHECK_PENDING")
        now = (
            datetime.now(timezone.utc)
            if self.time_authority is None
            else self.time_authority.now_corrected_utc()
        )
        self._assessment_valid_until = now + timedelta(seconds=self._startup_validity_window_s())

    def _startup_validity_window_s(self) -> float:
        """Keep the previous assessment alive through one inspection budget."""
        return (
            self.config.startup.monitor_interval_s + self.config.startup.inspection_budget_s
        )

    def _seconds_until_next_inspection(self) -> float:
        """Start the next read one budget before the current assessment expires."""
        until = self._assessment_valid_until
        if until is None:
            return self.config.startup.monitor_interval_s
        remaining = (until - self._corrected_now()).total_seconds()
        if remaining <= 0:
            return 0.0
        lead = remaining - self.config.startup.inspection_budget_s
        if lead > 0:
            return lead
        return min(self.config.startup.monitor_interval_s, remaining)

    async def _startup_monitor_loop(self) -> None:
        while not self.stop_requested:
            delay = self._seconds_until_next_inspection()
            if delay > 0:
                await asyncio.sleep(delay)
            if self.stop_requested:
                return
            expired = False
            until = self._assessment_valid_until
            if until is not None and self._corrected_now() >= until:
                expired = True
            try:
                assessment = await self._assess_startup(write_report=False)
            except Exception as exc:  # noqa: BLE001 - recorded, readiness stays blocked
                reason = self._record_recheck_failure([], error=f"{type(exc).__name__}: {exc}")
                self._invalidate_startup_recheck(
                    "STARTUP_ASSESSMENT_EXPIRED",
                    f"startup recheck failed: {reason}",
                )
                continue
            publication = self._last_inspection_publication
            if publication in {"discarded", "withheld"}:
                if expired:
                    self._invalidate_startup_recheck(
                        "STARTUP_ASSESSMENT_EXPIRED",
                        "startup recheck could not be published before it expired",
                    )
                continue
            blocking = self._recheck_blocking_issues(assessment)
            if publication == "accepted" and not blocking:
                self._last_recheck_failure_key = None
                self._mark_startup_fresh()
            else:
                reason = self._record_recheck_failure(
                    blocking,
                    error=None if publication == "accepted" else f"publication:{publication}",
                )
                self._invalidate_startup_recheck(
                    "STARTUP_CHECK_PENDING",
                    f"startup recheck is not ready: {reason}",
                )

    def _invalidate_startup_recheck(self, blocker: str, message: str) -> None:
        self._startup_invalidated_reason = "STARTUP_RECHECK_FAILED"
        self._assessment_valid_until = None
        self.capabilities.set_blocker(blocker, message)

    def _recheck_blocking_issues(self, assessment: Any) -> list[Any]:
        """Issues that fail a startup recheck (not AUTO, not the active session's own exposure)."""
        if assessment.ready:
            return []
        active = self.lifecycle.session_id
        blocking = []
        for issue in assessment.issues:
            if issue.severity.value == "AUTO":
                continue
            if (
                issue.code in {"OWNED_POSITION", "OWNED_LIVE_ORDER"}
                and issue.session_id == active
            ):
                continue
            blocking.append(issue)
        return blocking

    def _recheck_keeps_readiness(self, assessment: Any) -> bool:
        return not self._recheck_blocking_issues(assessment)

    def _record_recheck_failure(self, issues: list[Any], *, error: str | None = None) -> str:
        """Record why a startup recheck failed (once per distinct cause); return a summary."""
        details = [
            {
                "code": issue.code,
                "severity": issue.severity.value,
                "session_id": issue.session_id,
                "summary": issue.summary,
            }
            for issue in issues
        ]
        key = (error, tuple(sorted((d["code"], d["session_id"] or "") for d in details)))
        if key != self._last_recheck_failure_key:
            self._last_recheck_failure_key = key
            self.run_recorder.record(
                "STARTUP_RECHECK_FAILED",
                {"issues": details, "error": error},
            )
        codes = ", ".join(
            f"{d['code']}({(d['session_id'] or '-')[-8:]})" for d in details
        )
        return codes or (error or "unknown")

    def _sessions_for_inspection(self):
        """Restored sessions plus anything the running coordinator already owns."""
        by_id = {}
        for session in self.restored_sessions:
            if session.session_id:
                by_id[session.session_id] = session
        for session_id, session in self.coordinator.states.items():
            by_id[session_id] = session
        return tuple(by_id.values())

    def _merge_inspection_sessions(self, reported):
        merged = []
        seen: set[str] = set()
        for session in reported:
            current = self.coordinator.states.get(session.session_id or "")
            chosen = session if current is None else current
            session_id = chosen.session_id
            if session_id and session_id in seen:
                continue
            if session_id:
                seen.add(session_id)
            merged.append(chosen)
        for session in self._sessions_for_inspection():
            session_id = session.session_id
            if session_id and session_id not in seen:
                merged.append(session)
                seen.add(session_id)
        return tuple(merged)

    async def _assess_startup(self, *, write_report: bool = True):
        from dataclasses import replace

        from tyrex_pm.execution.activity_checkpoint import save_activity_ledger
        from tyrex_pm.execution.inspection_sync import (
            authored_publication_holds,
            publication_decision,
        )
        from tyrex_pm.execution.startup_reconciliation import (
            AccountIdentity,
            InspectionFacts,
            ReadCoverage,
            assess,
            evidence_disposition,
        )
        from tyrex_pm.reporting.startup_report import startup_report_payload, write_startup_report
        from tyrex_pm.runtime.startup_controller import StartupController

        started_epoch = _try_execution_epoch(self.coordinator)
        facts = await self._read_startup_facts()
        owned_sessions = self._sessions_for_inspection()
        if facts is None:
            facts = InspectionFacts(
                identity=None,
                coverage=ReadCoverage(
                    complete=False,
                    positions_complete=False,
                    orders_complete=False,
                    activity_complete=False,
                    reason="startup inspection exceeded its budget",
                ),
                sessions=owned_sessions,
            )
        else:
            facts = replace(facts, sessions=self._merge_inspection_sessions(facts.sessions))
        if facts.identity is None and facts.coverage.complete:
            facts = replace(
                facts,
                identity=AccountIdentity(
                    chain_id=137, environment="production", wallet="unspecified"
                ),
            )
        if self.settlement_hooks is not None:
            facts = replace(
                facts,
                committed_holds=self.settlement_hooks.committed_holds(
                    include_settled=self.continuous
                ),
                committed_holds_block_entries=not self.continuous,
            )
        self._startup_wallet = None if facts.identity is None else facts.identity.wallet
        controller = StartupController(
            coordinator=self.coordinator,
            generation=self.run_instance_id,
        )
        await self.coordinator.start()
        async with self.coordinator.evidence_gate:
            ended_epoch = _try_execution_epoch(self.coordinator)
            if started_epoch is None or ended_epoch is None:
                execution = "discarded"
            else:
                execution = publication_decision(started_epoch, ended_epoch)
            evidence = evidence_disposition(facts)
            publication = execution if execution != "accepted" else evidence
            checkpoint = "unchanged"
            if publication != "accepted":
                assessment = assess(facts)
            else:
                before_unresolved = ended_epoch.unresolved_attempt_ids
                before_sequence = ended_epoch.journal_sequence
                assessment = await controller.inspect(facts)
                after_epoch = _try_execution_epoch(self.coordinator)
                observed = frozenset(
                    self.coordinator.journal.event_ids_after(before_sequence)
                )
                consistent = after_epoch is not None and authored_publication_holds(
                    before_unresolved=before_unresolved,
                    after_unresolved=after_epoch.unresolved_attempt_ids,
                    authored_event_ids=frozenset(controller.published_event_ids),
                    observed_event_ids=observed,
                )
                ledger = facts.activity_ledger
                if consistent and ledger is not None:
                    save_activity_ledger(self.coordinator.journal, ledger)
                    checkpoint = "advanced"
                elif not consistent:
                    publication = "discarded"
        self._last_inspection_publication = publication
        self._record_inspection_timing(
            facts,
            publication,
            operation_id=str(uuid4()),
            execution=execution,
            evidence=evidence,
            checkpoint=checkpoint,
            ready=bool(assessment.ready and publication == "accepted"),
        )
        if write_report:
            venue = getattr(self.config, "venue", None)
            armed = self.capabilities.live_requested
            payload = startup_report_payload(
                assessment,
                run_name=self.config.run_name,
                live_requested=armed,
                venue=venue,
                real_orders_authorized=venue == "live" and armed,
            )
            self.startup_report = payload
            write_startup_report(self.output_directory / "startup_report.json", payload)
        return assessment

    def _record_inspection_timing(
        self,
        facts: Any,
        publication: str,
        *,
        operation_id: str,
        execution: str,
        evidence: str,
        checkpoint: str,
        ready: bool,
    ) -> None:
        coverage = None if facts is None else getattr(facts, "coverage", None)
        duration_ms = None if facts is None else getattr(facts, "inspection_duration_ms", None)
        pages = 0 if facts is None else int(getattr(facts, "activity_pages", 0) or 0)
        mode = "" if facts is None else str(getattr(facts, "activity_mode", "") or "")
        payload = {
            "operation_id": operation_id,
            "duration_ms": duration_ms,
            "activity_pages": pages,
            "activity_mode": mode,
            "execution": execution,
            "evidence": evidence,
            "publication": publication,
            "checkpoint": checkpoint,
            "ready": ready,
            "activity_complete": bool(coverage is not None and coverage.activity_complete),
            "coverage_complete": bool(coverage is not None and coverage.complete),
        }
        try:
            self.run_recorder.record("INSPECTION_TIMING", payload)
        except Exception:
            return

    async def _read_startup_facts(self):
        import inspect

        reader = getattr(self.gateway, "startup_inspection", None)
        if reader is None:
            from tyrex_pm.execution.startup_reconciliation import InspectionFacts, ReadCoverage

            return InspectionFacts(
                identity=None,
                coverage=ReadCoverage(
                    complete=False,
                    positions_complete=False,
                    orders_complete=False,
                    activity_complete=False,
                    reason="startup reader is not available",
                ),
                sessions=self._sessions_for_inspection(),
            )
        kwargs: dict[str, Any] = {}
        parameters = inspect.signature(reader).parameters
        if "journal" in parameters:
            kwargs["journal"] = self.coordinator.journal
        if "sessions" in parameters:
            kwargs["sessions"] = self._sessions_for_inspection()

        async def _call():
            result = reader(**kwargs)
            if asyncio.iscoroutine(result):
                return await result
            return result

        try:
            return await asyncio.wait_for(_call(), timeout=self.config.startup.inspection_budget_s)
        except TimeoutError:
            return None

    async def _resolve_startup_action(self, assessment: Any) -> Any:
        from tyrex_pm.application.startup_interaction import proposal_for, request_decision
        from tyrex_pm.core.ids import CorrelationId, MarketId, StrategyId
        from tyrex_pm.runtime.startup_controller import StartupController

        decision = await request_decision(
            assessment,
            live=True,
            reader=self.decision_reader or _tty_decision_reader,
            timeout_s=self.config.startup.prompt_timeout_s,
        )
        proposal = proposal_for(assessment, decision.issue_id)
        if not decision.approved or proposal is None:
            return assessment
        controller = StartupController(
            coordinator=self.coordinator,
            generation=self.run_instance_id,
        )
        book_provider = getattr(self.gateway, "recovery_book", None)
        if not callable(book_provider):
            return assessment
        if not await controller.authorize(proposal, approved=True, live=True):
            return assessment

        async def _abort() -> Any:
            await controller.finish(proposal, "aborted")
            return assessment

        fresh = await self._read_startup_facts()
        observed = _recovery_observation(fresh, proposal)
        if observed is None:
            return await _abort()
        observed_shares, lifecycle_status = observed
        reason = controller.refresh_still_valid(
            proposal,
            shares=observed_shares,
            condition_id=proposal.condition_id,
            token_id=proposal.token_id,
            lifecycle_status=lifecycle_status,
        )
        if reason is not None:
            return await _abort()
        if observed_shares == 0 and proposal.action != "cancel":
            return await _abort()
        try:
            book = await book_provider(proposal.condition_id, proposal.token_id)
        except Exception:
            return await _abort()
        if book is None:
            return await _abort()
        self._recovery_book = lambda: book
        self._recovery_binding_id = book.binding_id
        saved_reads = self.capabilities.account_reads_ready
        saved_market = self.capabilities.active_market_ready
        saved_sellable = self.capabilities.selected_token_sellable
        try:
            self.coordinator.recovery_now = (
                (lambda: datetime.now(timezone.utc))
                if self.time_authority is None
                else self.time_authority.now_corrected_utc
            )
            if self.lifecycle.session_id is None:
                self.lifecycle.resume(proposal.session_id)
            self.capabilities.account_reads_ready = True
            self.capabilities.active_market_ready = True
            self.capabilities.selected_token_sellable = True
            intent = FlattenIntent(
                intent_id=new_intent_id(),
                strategy_id=StrategyId(self.config.strategy_kind),
                instrument_id=InstrumentId(proposal.token_id),
                market_id=MarketId(proposal.condition_id),
                created_at=datetime.now(timezone.utc),
                correlation_id=CorrelationId(proposal.issue_id),
                causation_id=None,
                reason_code="APPROVED_RECOVERY",
            )
            legs = (getattr(book, "up", None), getattr(book, "down", None))
            leg = next(
                (
                    candidate
                    for candidate in legs
                    if getattr(candidate, "token_id", None) == proposal.token_id
                ),
                None,
            )
            min_size = None if leg is None else getattr(leg, "min_order_size", None)
            bid = None if leg is None else getattr(getattr(leg, "quote", None), "best_bid", None)
            if proposal.action != "cancel" and (
                min_size is None or bid is None or Decimal(str(bid)) < proposal.min_price
            ):
                return await _abort()
            await self.lifecycle.submit_exit(
                intent,
                token_id=proposal.token_id,
            min_order_size=None if min_size is None else Decimal(str(min_size)),
            last_bid=None if bid is None else Decimal(str(bid)),
                historical_recovery=True,
            )
            await controller.finish(proposal, "submitted")
            state = self.lifecycle.state
            self.recovery_sessions.append(
                {
                    "session_id": proposal.session_id,
                    "condition_id": proposal.condition_id,
                    "action": proposal.action,
                    "max_shares": str(proposal.max_shares),
                    "phase": None if state is None else state.phase.value,
                }
            )
            if state is not None and state.safe_to_open_entry:
                self.lifecycle.release_terminal()
        finally:
            self.capabilities.account_reads_ready = saved_reads
            self.capabilities.active_market_ready = saved_market
            self.capabilities.selected_token_sellable = saved_sellable
            self._recovery_book = None
            self._recovery_binding_id = None
        return await self._assess_startup()

    async def _stop_startup_monitor(self) -> None:
        task = self._startup_monitor
        self._startup_monitor = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
