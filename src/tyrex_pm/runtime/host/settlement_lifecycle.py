"""Hold-to-resolution and settlement: holds, detach, residual hold, settlement worker."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tyrex_pm.core.intents import (
    HoldToResolutionIntent,
)
from tyrex_pm.execution.session_state import (
    ExecutionSessionState,
)
from tyrex_pm.runtime.host.shared import (
    _TRANSIENT_HOLD_REFUSALS,
    _chain_payout_reader,
    _chain_reader,
    _hold_committed,
)


class SettlementLifecycleMixin:
    """Hold-to-resolution and settlement: holds, detach, residual hold, settlement worker.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    async def _preserve_uncommitted_hold(self, reason: str) -> bool:
        """Do not sell a filled settlement episode just because the process is stopping.

        A proven hold policy is committed if its prerequisites still hold, and is
        otherwise left in place. A missing or conflicting policy is also left in
        place. A managed-exit episode keeps the documented shutdown flatten.
        """
        hooks = self.settlement_hooks
        market = self.active_market
        if hooks is None or market is None or not hasattr(hooks, "current_episode"):
            return False
        episode = hooks.current_episode(market.window_id)
        if episode is None:
            return False
        state = getattr(episode, "state", None)
        state_value = state.value if hasattr(state, "value") else state
        if state_value not in ("FILLED", "HOLD_REQUESTED"):
            return False
        policy = getattr(episode, "lifecycle_policy", None)
        unresolved = self._recovery_policy_unresolved
        if policy != "hold_to_resolution" and not unresolved:
            return False
        if policy == "hold_to_resolution" and not unresolved:
            await self._commit_hold(market.window_id, source="shutdown_recovery")
        preserved = _hold_committed(self)
        if preserved:
            await self._cancel_for_hold()
        self.run_recorder.record(
            "SHUTDOWN_HOLD_PRESERVED" if preserved else "SHUTDOWN_HOLD_UNCOMMITTED",
            {"reason": reason, "policy": policy},
        )
        return True

    async def _cancel_for_hold(self) -> Any:
        """Cancel owned resting orders and return the reconciled state (no sells)."""
        if self.lifecycle.session_id is None:
            # The committed hold was already detached (continuous run): no session and no
            # resting order remain; the settlement ledger owns the position.
            return None
        await self.lifecycle._cancel_open_orders(reason="hold_to_resolution")
        return await self.lifecycle.reconcile()

    def _strategy_fee_curve(self) -> Any:
        runtime = self._market_runtime_ref
        if runtime is None or self.active_market is None:
            return None
        driver = runtime._strategy_by_window.get(self.active_market.window_id) or runtime.driver
        return None if driver is None else driver.fee_curve

    async def _settlement_before_entry(
        self, intent: Any, token_id: str, instrument: Any, *, account_state: Any = None
    ) -> tuple[str, ...] | None:
        """Settlement gate. Returns the blocking reasons (recorded), or None when reserved."""
        assert self.settlement_hooks is not None and self.active_market is not None
        episode_seq, attempt_seq = self._intent_episode(intent)
        collateral = None if account_state is None else getattr(
            account_state, "collateral_balance", None
        )
        session = self.active_market
        market = session.market
        binding = getattr(session, "binding", None)
        outcome_index = None
        for leg in () if binding is None else binding.outcomes.legs:
            if leg.token_id.value == token_id:
                outcome_index = leg.outcome_index
        if outcome_index is None:
            check_reasons: tuple[str, ...] = ("OUTCOME_INDEX_UNKNOWN",)
            evidence: dict[str, Any] = {}
        else:
            check = await self.settlement_hooks.before_entry(
                window_id=session.window_id,
                window_slug=session.window_id,
                market_id=market.market_id.value,
                condition_id=market.condition_id,
                token_id=token_id,
                outcome_index=outcome_index,
                target_notional=intent.target_notional,
                tick_size=market.tick_size,
                min_order_size=market.min_order_size,
                strategy_fee_curve=self._strategy_fee_curve(),
                episode_seq=episode_seq,
                attempt_seq=attempt_seq,
                lifecycle_policy=getattr(self.config, "lifecycle_mode", None),
                entry_started_at=self._driver_episode_started_at(session.window_id),
                available_collateral=collateral,
                collateral_read_started_at=None
                if account_state is None
                else getattr(account_state, "collateral_read_started_at", None),
            )
            if check.allowed:
                self.run_recorder.record("SETTLEMENT_EPISODE_RESERVED", check.evidence)
                return None
            check_reasons, evidence = check.reasons, check.evidence
        self.run_recorder.record(
            "INTENT_BLOCKED",
            {
                "intent_id": intent.intent_id.value,
                "intent_type": type(intent).__name__,
                "reason": "SETTLEMENT_ENTRY_BLOCKED",
                "settlement_reasons": list(check_reasons),
                **evidence,
            },
        )
        return tuple(check_reasons)

    def _position_cost(self, state: ExecutionSessionState | None) -> Decimal:
        if state is None:
            return Decimal("0")
        return sum(
            (trade.shares * trade.price for trade in state.trades.values() if trade.side == "BUY"),
            Decimal("0"),
        )

    async def _consume_hold(self, intent: HoldToResolutionIntent) -> None:
        await self._commit_hold(
            intent.window_id,
            source="strategy_intent",
            intent_id=intent.intent_id.value,
            extra={
                "reason_code": intent.reason_code,
                "market_id": intent.market_id.value,
                "instrument_id": intent.instrument_id.value,
            },
        )

    def _host_commits_hold(self) -> bool:
        """Hold profile: the host commits a filled position itself (no strategy turn)."""
        if self.settlement_hooks is None or getattr(self.config, "lifecycle_mode", None) != (
            "hold_to_resolution"
        ):
            return False
        strategy_exit = getattr(getattr(self.config, "strategy", None), "exit", None)
        return getattr(strategy_exit, "policy", None) == "hold_to_resolution"

    async def _commit_hold(
        self,
        window_id: str,
        *,
        source: str,
        intent_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Commit the window's filled episode to resolution (idempotent).

        Keeps every precondition of ``SettlementHooks.commit_hold`` (confirmed inventory
        matching the ledger, conflicting orders cancelled, no unresolved submission).
        A transient refusal is retried on the next tick; a committed hold is never sold
        and survives rollover and shutdown (detached to the settlement ledger).
        """
        committed, detail = (False, "LIFECYCLE_NOT_SETTLEMENT_PROFILE")
        if self._recovery_policy_unresolved:
            detail = "RECOVERY_POLICY_UNRESOLVED"
        elif self.settlement_hooks is not None:
            committed, detail = await self.settlement_hooks.commit_hold(
                window_id=window_id,
                state=self.lifecycle.state,
                cancel_conflicting=self._cancel_for_hold,
            )
        if committed or self._hold_commit_noted != (window_id, detail):
            self._hold_commit_noted = (window_id, detail)
            self.run_recorder.record(
                "HOLD_TO_RESOLUTION",
                {"intent_id": intent_id, "window_id": window_id, "source": source,
                 "committed": committed, "detail": detail, **(extra or {})},
            )
        if not committed:
            if source == "host_after_fill" and detail in _TRANSIENT_HOLD_REFUSALS:
                self._pending_hold_commit = window_id
            return False
        self._pending_hold_commit = None
        driver = self._driver_for(window_id)
        if driver is not None:
            driver.on_hold_committed()
            self._capture_feedback(window_id, "hold", {})
        state = self.lifecycle.state
        if state is not None:
            self.protection_registry.disarm(state.session_id)
        if self.continuous:
            # The holding now belongs to the settlement ledger (resolution worker); the
            # lifecycle is freed for the next window once its trades are confirmed.
            self._pending_detach = window_id
            await self._try_detach()
            return True
        # The single episode is complete for this process; the holding now
        # waits for resolution under `tyrex-pm settlement`.
        self.stop_requested = True
        return True

    async def _try_detach(self) -> bool:
        """Detach a committed hold once nothing about it is pending (e.g. a matched trade
        still awaiting venue confirmation). Retried every tick; until then the window
        cannot be promoted, and the hold stays protected from any sale."""
        window = self._pending_detach
        if window is None or self.lifecycle.state is None:
            self._pending_detach = None
            return True
        state = await self.lifecycle.reconcile()
        # The tick and the strategy path both retry the detach; the other one may have
        # finished it while this one awaited the reconciliation.
        if self._pending_detach != window or self.lifecycle.state is None:
            return True
        try:
            detached = self.lifecycle.detach_committed(reason="hold_committed")
        except RuntimeError as exc:
            if self._detach_noted != (window, str(exc)):
                self._detach_noted = (window, str(exc))
                self.run_recorder.record(
                    "COMMITTED_HOLD_DETACH_PENDING",
                    {"window_id": window, "reason": str(exc),
                     "phase": None if state is None else state.phase.value},
                )
            return False
        self.run_recorder.record(
            "COMMITTED_HOLD_DETACHED", {"session_id": detached, "window_id": window}
        )
        self._pending_detach = None
        self.selected_instrument = None
        self.selected_token_id = None
        return True

    async def _shutdown_residual_hold(self, reason: str) -> bool:
        """managed_exit: at stop, remaining inventory follows the declared residual policy
        (hold to resolution) instead of an unannounced sale. Falls back to the drain."""
        if self.config.lifecycle_mode != "managed_exit" or self.settlement_hooks is None:
            return False
        state = self.lifecycle.state
        if state is None or not state.has_exposure or self.active_market is None:
            return False
        committed, detail = await self.settlement_hooks.commit_hold(
            window_id=self.active_market.window_id,
            state=state,
            cancel_conflicting=self._cancel_for_hold,
        )
        self.run_recorder.record(
            "SHUTDOWN_RESIDUAL_HOLD",
            {"reason": reason, "committed": committed, "detail": detail},
        )
        return committed

    def _build_settlement_worker(self) -> Any:
        """Automatic resolution/payout tracking for continuous runs (None otherwise)."""
        hooks = self.settlement_hooks
        settlement = getattr(self.config, "settlement", None)
        if hooks is None or settlement is None or not self.continuous:
            return None
        from tyrex_pm.execution.settlement.resolution import ResolutionManager
        from tyrex_pm.runtime.settlement_worker import SettlementWorker

        reader = getattr(self.gateway, "payout_reader", None)
        chain_reader = None
        if reader is None:
            chain_reader = _chain_reader(self.polygon_rpc_url)
            reader = _chain_payout_reader(self.polygon_rpc_url, chain_reader)
        if reader is None:
            self.run_recorder.record(
                "SETTLEMENT_WORKER_UNAVAILABLE",
                {"reason": "no Polygon RPC configured (TYREX_POLYGON_RPC_URL)",
                 "effect": "holdings stay unresolved; unresolved caps pause entries"},
            )
            return None
        paper = getattr(self.config, "paper", None)
        verifier = getattr(self.gateway, "verify_redemption", None)
        wallet = getattr(self.gateway, "wallet", None) if paper is not None else (
            settlement.redemption_wallet
        )
        manager = ResolutionManager(
            hooks.ledger,
            payout_reader=reader,
            redemption_verifier=verifier if paper is not None else None,
            wallet=wallet,
        )
        redeemer = None
        if paper is not None and paper.auto_redeem_after_s is not None:
            from tyrex_pm.execution.paper import delayed_paper_redeemer

            redeemer = delayed_paper_redeemer(self.gateway, paper.auto_redeem_after_s)
        discovery = None
        options = getattr(settlement, "redemption_discovery", None)
        if (paper is None and chain_reader is not None and wallet
                and options is not None and options.enabled):
            # The trading process owns the ledger while it runs (state lock held):
            # discovery runs in this worker, outside evaluation and order submission.
            from tyrex_pm.execution.settlement.redemption_discovery import (
                DiscoveryPolicy,
                RedemptionDiscovery,
            )

            discovery = RedemptionDiscovery(
                manager, chain_reader, wallet=wallet,
                state_path=settlement.ledger_path.parent / "redemption_discovery.json",
                policy=DiscoveryPolicy(
                    interval_s=options.interval_s,
                    max_blocks_per_pass=options.max_blocks_per_pass,
                    max_log_calls_per_pass=options.max_log_calls_per_pass),
                record=self.run_recorder.record)
        return SettlementWorker(
            manager=manager,
            interval_s=settlement.resolution_poll_interval_s,
            record=self.run_recorder.record,
            paper_redeemer=redeemer,
            discovery=discovery,
        )
