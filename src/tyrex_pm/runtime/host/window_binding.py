"""Window binding and rotation, per-window driver access and summaries."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
)
from tyrex_pm.execution.exit_sizing import (
    is_exempt_prior_position_blocker,
)
from tyrex_pm.execution.session_state import (
    ExecutionSessionState,
)
from tyrex_pm.protection import (
    ProtectionRegistry,
)
from tyrex_pm.runtime.host.shared import (
    LATE_EVIDENCE_POLL_S,
    LATE_EVIDENCE_WATCH_S,
)
from tyrex_pm.runtime.settlement_hooks import startup_exposure_decision
from tyrex_pm.strategies.base import EpisodeView


class WindowBindingMixin:
    """Window binding and rotation, per-window driver access and summaries.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def can_promote(self) -> bool:
        state = self.lifecycle.state
        if state is not None and not state.safe_to_open_entry:
            return False
        return not self.lifecycle.unresolved_window_sessions()

    def _current_entry_tau_s(self) -> float | None:
        if self.active_market is None or self.active_market.market.event_end is None:
            return None
        if self.time_authority is None:
            return None
        now = self.time_authority.now_corrected_utc()
        return (self.active_market.market.event_end - now).total_seconds()

    async def _inspect_retained_session(
        self,
        restored: ExecutionSessionState,
        *,
        bound_market_id: str,
    ) -> str:
        identity = restored.identity
        payload = {
            "session_id": restored.session_id,
            "phase": restored.phase.value,
            "confirmed_position_shares": str(restored.confirmed_position_shares),
            "open_venue_order_ids": list(restored.resting_limit_venue_ids),
            "mutation_ambiguous": restored.has_unresolved_mutation_attempt,
            "match_awaiting": restored.entry_match_awaiting_confirmation,
        }
        if identity is None:
            payload["host_action"] = "missing_identity"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "missing_identity"
        payload.update(
            {
                "market_id": identity.market_id,
                "window_id": identity.window_id,
                "token_id": identity.token_id,
            }
        )
        same_market = identity.market_id == bound_market_id
        hooks = self.settlement_hooks
        if (
            not same_market
            and self.continuous
            and hooks is not None
            and (identity.market_id, identity.token_id)
            in hooks.committed_holds(include_settled=True)
            and not restored.has_journal_uncertainty
        ):
            payload["host_action"] = "committed_hold_tracked"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "foreign_inventory_absent"
        if same_market:
            host_action = "resume_flatten" if restored.has_exposure else (
                "resume_cancel_then_prove"
                if restored.working_maker_entry or restored.resting_limit_venue_ids
                else "block_manual"
            )
            payload["host_action"] = host_action
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return host_action
        if restored.has_journal_uncertainty:
            payload["host_action"] = "block_foreign"
            payload["block_reason"] = "journal_uncertainty"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "block_foreign"
        snapshot = await self.account_state_authority.observe(
            market_id=identity.market_id,
            token_ids=(identity.token_id,),
            required_collateral=self.config.risk.maximum_total_debit,
        )
        payload["scoped_snapshot_status"] = snapshot.status.value
        payload["scoped_snapshot_market_id"] = snapshot.market_id
        payload["scoped_blockers"] = list(snapshot.blockers)
        if (
            snapshot.status
            in {AccountSnapshotStatus.UNAVAILABLE, AccountSnapshotStatus.STALE}
            or snapshot.market_id != identity.market_id
            or identity.token_id not in snapshot.token_ids
            or not snapshot.read_complete
        ):
            payload["host_action"] = "block_foreign"
            payload["block_reason"] = "snapshot_incomplete"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "block_foreign"
        leftover = []
        for blocker in snapshot.blockers:
            if blocker.startswith("PRIOR_OPEN_ORDER"):
                leftover.append(blocker)
            elif blocker.startswith("PRIOR_POSITION") and not is_exempt_prior_position_blocker(
                blocker, self.known_dust_residuals
            ):
                leftover.append(blocker)
        if leftover:
            payload["host_action"] = "block_foreign"
            payload["block_reason"] = "scoped_leftover"
            self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
            return "block_foreign"
        payload["host_action"] = "foreign_inventory_absent"
        self.run_recorder.record("PRIOR_SESSION_INSPECTED", payload)
        return "foreign_inventory_absent"

    async def _bind_pending_market(self) -> None:
        session = self.pending_market
        if session is None or session is self.active_market:
            return
        state = self.lifecycle.state
        if state is not None and not state.safe_to_open_entry:
            return
        if not self.can_promote():
            return
        if state is not None:
            self.lifecycle.release_terminal()
            self.selected_instrument = None
            self.selected_token_id = None
        previous_market_id = None
        if self.active_market is not None:
            previous_market = self.active_market.market
            previous_market_id = previous_market.condition_id or previous_market.market_id.value
            self._summarize_window(self.active_market.window_id, "promoted")
        self.lifecycle.new_window()
        self.protection_registry = ProtectionRegistry()
        self.pending_market = None
        self.capabilities.active_market_ready = False
        self.capabilities.account_reads_ready = False
        self.capabilities.user_stream_ready = False
        self.capabilities.prior_scope_clear = False
        self.capabilities.entry_allowance_ready = False
        self.capabilities.collateral_ready = False
        self.capabilities.order_metadata_ready = False
        await self.gateway.stop_user_stream()
        market = session.market
        market_id = market.condition_id or market.market_id.value
        token_ids = (market.yes.token_id.value, market.no.token_id.value)
        account_state = self.account_state_authority.current(market_id)
        self.active_market = session
        self.capabilities.active_market_ready = True
        self._apply_account_snapshot(account_state)
        self._refresh_order_metadata_capability()
        if not self.capabilities.order_metadata_ready:
            self._schedule_order_metadata_warm(session)
        matching_restored = [
            restored
            for restored in self.restored_sessions
            if restored.identity is not None
            and restored.identity.market_id in {market.condition_id, market.market_id.value}
            and restored.identity.token_id in token_ids
        ]
        foreign_restored = [
            restored for restored in self.restored_sessions if restored not in matching_restored
        ]
        blocking_foreign: list[ExecutionSessionState] = []
        for restored in foreign_restored:
            host_action = await self._inspect_retained_session(restored, bound_market_id=market_id)
            if host_action != "foreign_inventory_absent":
                blocking_foreign.append(restored)
        if blocking_foreign:
            self.prior_session_risk = True
            self.capabilities.set_blocker(
                "UNRESOLVED_PRIOR_MARKET_SESSION",
                ",".join(str(restored.session_id) for restored in blocking_foreign),
            )
        else:
            self.capabilities.clear_blocker("UNRESOLVED_PRIOR_MARKET_SESSION")
        for restored in matching_restored:
            await self._inspect_retained_session(restored, bound_market_id=market_id)
        if len(matching_restored) > 1:
            self.prior_session_risk = True
            self.capabilities.set_blocker("MULTIPLE_RESTORED_SESSIONS")
        else:
            self.capabilities.clear_blocker("MULTIPLE_RESTORED_SESSIONS")
        if matching_restored:
            self.prior_session_risk = True
        restored = matching_restored[0] if len(matching_restored) == 1 else None
        self.stream_session_id = (
            str(restored.session_id)
            if restored is not None
            else f"{self.config.run_name}-{uuid4()}"
        )
        self.lifecycle.stream_alias = self.stream_session_id
        self._window_started_at[session.window_id] = self._corrected_now().isoformat()
        self._seed_driver_from_ledger(session.window_id)
        await self.gateway.start_user_stream(
            session_id=self.stream_session_id,
            markets=(market.condition_id or market.market_id.value,),
            on_evidence=self.lifecycle.on_stream_evidence,
            on_gap=self.lifecycle.on_stream_gap,
            on_ready=self.lifecycle.on_stream_ready,
        )
        self.run_recorder.record(
            "MARKET_BOUND",
            {
                "market_id": market.market_id.value,
                "condition_id": market.condition_id,
                "window_id": session.window_id,
                "entry_enabled": session.entry_enabled,
                "stream_session_id": self.stream_session_id,
                "account_state_status": account_state.status.value,
                "account_state_observed_at": account_state.observed_at,
                "account_state_blockers": list(account_state.blockers),
                "restored_session_count": len(matching_restored),
            },
        )
        self._record_readiness_if_changed()
        if previous_market_id is not None and previous_market_id != market_id:
            self.account_state_authority.retire(previous_market_id)
        if restored is not None:
            self.lifecycle.resume(self.stream_session_id)
            assert restored.identity is not None
            self.selected_token_id = restored.identity.token_id
            self.selected_instrument = next(
                (
                    instrument
                    for instrument in (market.yes, market.no)
                    if instrument.token_id.value == self.selected_token_id
                ),
                None,
            )
            reconciled = await self.lifecycle.reconcile()
            if reconciled is not None and reconciled.terminal:
                self.restored_sessions.remove(restored)
            if reconciled is None or not reconciled.reconciliation_complete:
                self.capabilities.set_blocker("PRIOR_SESSION_RECONCILIATION_INCOMPLETE")
            elif reconciled.has_exposure and self.selected_instrument is not None:
                self.capabilities.selected_token_sellable = reconciled.sellable_shares > 0
                episode = (
                    None
                    if self.settlement_hooks is None or self.active_market is None
                    else self.settlement_hooks.current_episode(self.active_market.window_id)
                )
                episode_state = (
                    None if episode is None or episode.state is None else episode.state.value
                )
                decision = startup_exposure_decision(
                    has_exposure=True,
                    episode_state=episode_state,
                    stored_policy=None if episode is None else episode.lifecycle_policy,
                    loaded_policy=getattr(self.config, "lifecycle_mode", None),
                )
                self._recovery_policy_unresolved = decision == "block"
                self.run_recorder.record(
                    "SETTLEMENT_EXPOSURE_RECOVERED",
                    {
                        "action": decision,
                        "episode_state": episode_state,
                        "stored_policy": None if episode is None else episode.lifecycle_policy,
                    },
                )
                if decision == "commit_hold" and self.active_market is not None:
                    self._pending_hold_commit = self.active_market.window_id
                elif (
                    decision == "track_hold"
                    and self.continuous
                    and self.active_market is not None
                ):
                    self._pending_detach = self.active_market.window_id
                elif decision == "block":
                    self.capabilities.set_blocker(
                        "RECOVERY_POLICY_UNRESOLVED",
                        "original lifecycle policy is missing or disagrees with the "
                        "loaded configuration",
                    )
                elif decision == "stop":
                    self.capabilities.set_blocker(
                        "STARTUP_ACTION_REQUIRED",
                        "owned exposure remains after startup assessment",
                    )
                    self.stop_requested = True
            elif reconciled.has_unresolved_mutation_attempt:
                await self.lifecycle.mark_manual("ambiguous_prior_submission")
                self.capabilities.set_blocker("AMBIGUOUS_PRIOR_SUBMISSION")
                self.stop_requested = True
            else:
                self.stop_requested = bool(reconciled is not None and reconciled.terminal)

    def _seed_driver_from_ledger(self, window_id: str) -> None:
        """Restart in the middle of a window: restore its durable attempt counters."""
        hooks = self.settlement_hooks
        driver = self._driver_for(window_id)
        if hooks is None or driver is None:
            return
        usage = hooks.ledger.window_usage(window_id)
        if usage.episodes == 0:
            return
        latest = hooks.ledger.by_window(window_id)
        started = None if latest is None else (latest.entry_started_at or latest.opened_at)
        if latest is None or latest.retry_clock == "none":
            backoff_state = "unknown" if latest is None else "none"
        elif latest.retry_clock == "deadline" and latest.retry_not_before:
            backoff_state = "deadline"
        else:
            backoff_state = "unknown"
        usage_input = dict(
            episodes=usage.episodes,
            attempts=usage.attempts,
            posts=usage.posts,
            filled_episodes=usage.filled_episodes,
            latest_attempts=0 if latest is None else latest.attempts,
            latest_state=None if latest is None or latest.state is None else latest.state.value,
            latest_posts=0 if latest is None else latest.posts,
            latest_prepare_failures=0 if latest is None else latest.prepare_failures,
            entry_started_at=started,
            started_known=bool(started),
            retry_not_before=None if latest is None else latest.retry_not_before,
            backoff_state=backoff_state,
            entry_side=None if latest is None else latest.entry_side,
            consecutive_service_failures=(
                None if latest is None else latest.consecutive_service_failures
            ),
        )
        driver.restore_window_usage(**usage_input)
        self._capture_feedback(window_id, "usage", usage_input)
        self.run_recorder.record(
            "WINDOW_BUDGET_RESTORED",
            {"window_id": window_id, "episodes": usage.episodes, "attempts": usage.attempts,
             "posts": usage.posts},
        )

    async def _check_earlier_attempts(self) -> None:
        """Late evidence for an earlier attempt of this window is a real exposure.

        It is attributed to its original session (stream routing), reconciled, and new
        entries stay blocked (``PRIOR_ATTEMPT_UNRESOLVED``) until it is resolved; a late
        fill on an attempt the ledger closed as no-fill needs an operator decision.
        """
        # Released attempts are polled (throttled) for a while: a fill reported only by
        # REST must not go unnoticed just because the stream missed it.
        for session_id in self.lifecycle.sessions_to_watch(
            watch_s=LATE_EVIDENCE_WATCH_S, interval_s=LATE_EVIDENCE_POLL_S
        ):
            try:
                await self.lifecycle.reconciler.reconcile(self.coordinator, session_id)
            except Exception as exc:  # noqa: BLE001
                self._record_runtime_error("earlier_attempt_watch", exc)
        unresolved = self.lifecycle.unresolved_window_sessions()
        if not unresolved:
            self.capabilities.clear_blocker("PRIOR_ATTEMPT_UNRESOLVED")
            return
        self.capabilities.set_blocker(
            "PRIOR_ATTEMPT_UNRESOLVED", ",".join(str(s.session_id) for s in unresolved)
        )
        for state in unresolved:
            if state.session_id is None:
                continue
            try:
                await self.lifecycle.reconciler.reconcile(self.coordinator, state.session_id)
            except Exception as exc:  # noqa: BLE001
                self._record_runtime_error("earlier_attempt_reconcile", exc)
            current = self.coordinator.states.get(state.session_id)
            if current is not None and current.has_exposure:
                self.run_recorder.record(
                    "LATE_FILL_DETECTED",
                    {"session_id": state.session_id,
                     "shares": str(current.confirmed_position_shares)},
                )
                hooks = self.settlement_hooks
                if hooks is not None and self.active_market is not None:
                    episode = hooks.current_episode(self.active_market.window_id)
                    if episode is not None and episode.state is not None and (
                        episode.state.value in ("NO_FILL", "NO_DISPATCH")
                    ):
                        hooks.ledger.record(
                            "MANUAL_INTERVENTION",
                            episode.episode_id,
                            key=f"MANUAL:late_fill:{state.session_id}",
                            reason="late_fill_after_no_fill",
                        )

    def _summarize_window(self, window_id: str, reason: str) -> None:
        if any(row["window_id"] == window_id for row in self.window_summaries):
            return
        from tyrex_pm.reporting.timezones import paris_iso

        now = self._corrected_now()
        summary: dict[str, Any] = {
            "window_id": window_id,
            "closed_by": reason,
            "bound_at": self._window_started_at.get(window_id),
            "summarized_at": now.isoformat(),
            "summarized_at_paris": paris_iso(now),
            "accounting_day_utc": now.date().isoformat(),
        }
        hooks = self.settlement_hooks
        if hooks is not None:
            usage = hooks.ledger.window_usage(window_id)
            # Ledger counts: an entry attempt is one authorized intent; it either reached
            # the venue (a POST) or was refused before sending (preparation failure or
            # final-gate refusal).
            summary.update(
                episodes=usage.episodes,
                filled_episodes=usage.filled_episodes,
                entry_attempts=usage.attempts,
                venue_posts=usage.posts,
                never_sent_attempts=max(0, usage.attempts - usage.posts),
                prepare_failures=usage.prepare_failures,
                states=[
                    e.state.value if e.state else None
                    for e in hooks.ledger.window_episodes(window_id)
                ],
            )
        view = self._episode_view(window_id)
        # Only report strategy state that belongs to this window (the driver may already
        # have been reset for the next one).
        if view is not None and view.window_id == window_id:
            summary.update(
                strategy_phase=view.phase,
                strategy_closed_reason=view.closed_reason,
                strategy_window_attempts=view.window_attempts,
            )
        self.window_summaries.append(summary)
        self.run_recorder.record("WINDOW_SUMMARY", summary)
        try:
            import json

            path = self.output_directory / "windows.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(summary, default=str) + "\n")
        except Exception as exc:  # noqa: BLE001 - reporting must not stop trading
            self._record_runtime_error("window_summary", exc)

    def _episode_view(self, window_id: str | None) -> EpisodeView | None:
        driver = self._driver_for(window_id)
        return None if driver is None else driver.episode_view()

    def _driver_episode_started_at(self, window_id: str) -> str | None:
        view = self._episode_view(window_id)
        return None if view is None else view.episode_started_at

    def _retry_clock_payload(self) -> dict[str, Any]:
        window_id = None if self.active_market is None else self.active_market.window_id
        view = self._episode_view(window_id)
        if view is None or view.phase != "REARMABLE":
            return {}
        deadline = view.next_attempt_not_before
        return {
            "entry_started_at": view.episode_started_at,
            "entry_side": view.entry_side,
            "retry_clock": "none" if not deadline else "deadline",
            "retry_not_before": deadline,
            "consecutive_service_failures": view.consecutive_service_failures,
        }

    def _driver_for(self, window_id: str | None) -> Any:
        runtime = self._market_runtime_ref
        if runtime is None or not hasattr(runtime, "window_driver"):
            return None
        return runtime.window_driver(window_id)
