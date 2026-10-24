"""Readiness projection: market data, account snapshot, owned shares."""

from __future__ import annotations

import traceback
from decimal import Decimal
from typing import Any

from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
    AccountStateSnapshot,
)
from tyrex_pm.execution.exit_sizing import (
    is_exempt_prior_position_blocker,
)
from tyrex_pm.market_data.book_health import ConnectionHealth, FeedSyncPhase, SyncHealth


class ReadinessMixin:
    """Readiness projection: market data, account snapshot, owned shares.

    Mixed into ``TradingRuntime``; reads and writes its fields.
    """

    def _prepare_account_state(self, market_session: Any, *, active: bool) -> None:
        market = market_session.market
        self.account_state_authority.prepare(
            market_id=market.condition_id or market.market_id.value,
            token_ids=(market.yes.token_id.value, market.no.token_id.value),
            required_collateral=self.config.risk.maximum_total_debit,
            active=active,
        )

    def _market_data_ready(self) -> bool:
        feed = None if self.feed_supervisor is None else self.feed_supervisor.active
        view = None if self.feed_supervisor is None else self.feed_supervisor.active_view()
        ready = bool(
            feed is not None
            and feed.phase is FeedSyncPhase.READY
            and feed.connection_health is ConnectionHealth.LIVE
            and view is not None
            and view.up.sync_health is SyncHealth.READY
            and view.down.sync_health is SyncHealth.READY
        )
        self.capabilities.public_feeds_ready = ready
        self.capabilities.books_ready = ready
        self.capabilities.book_desync_active = not ready
        effective = ready and self.capabilities.active_market_ready
        self._record_readiness_if_changed()
        return effective

    def _entry_evaluation_ready(self) -> bool:
        """Refresh all synchronous capability projections before evaluation."""
        if self.active_market is not None:
            market = self.active_market.market
            market_id = market.condition_id or market.market_id.value
            self._apply_account_snapshot(self.account_state_authority.current(market_id))
        return self._market_data_ready()

    def _record_readiness_if_changed(self) -> None:
        state = self.lifecycle.state
        snapshot = self.capabilities.snapshot(exposed=bool(state and state.has_exposure))
        payload = {
            "observable": snapshot.observable,
            "decision_ready": snapshot.decision_ready,
            "entry_executable": snapshot.entry_executable,
            "execution_infrastructure_ready": snapshot.execution_infrastructure_ready,
            "exit_executable": snapshot.exit_executable,
            "reconciliation_ready": snapshot.reconciliation_ready,
            "blockers": list(snapshot.blockers),
        }
        key = (
            snapshot.observable,
            snapshot.decision_ready,
            snapshot.execution_infrastructure_ready,
            snapshot.entry_executable,
            snapshot.exit_executable,
            snapshot.reconciliation_ready,
            snapshot.blockers,
        )
        if key != self._last_readiness_key:
            self._last_readiness_key = key
            self.run_recorder.record("READINESS_CHANGED", payload)

    def _record_runtime_error(self, stage: str, exc: BaseException) -> None:
        message = f"{stage}:{type(exc).__name__}:{exc}"
        self.runtime_errors.append(message)
        chain: list[dict[str, str]] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            chain.append(
                {
                    "error_type": type(current).__name__,
                    "message": str(current),
                }
            )
            current = current.__cause__ or current.__context__
        self.run_recorder.record(
            "RUNTIME_ERROR",
            {
                "stage": stage,
                "error_type": type(exc).__name__,
                "message": str(exc),
                "exception_chain": chain,
                "traceback": "".join(traceback.format_exception(exc)),
            },
        )

    def _apply_account_snapshot(self, snapshot: AccountStateSnapshot) -> None:
        """Apply one immutable account observation on the runtime's main loop."""
        self.account_state = snapshot
        complete = snapshot.read_complete
        self.capabilities.account_reads_ready = complete
        self.capabilities.collateral_ready = bool(
            complete
            and snapshot.collateral_balance is not None
            and snapshot.collateral_balance >= self.config.risk.maximum_total_debit
        )
        self.capabilities.entry_allowance_ready = bool(
            complete
            and snapshot.collateral_allowance is not None
            and snapshot.collateral_allowance >= self.config.risk.maximum_total_debit
        )
        prior_exposure_blockers = tuple(
            blocker
            for blocker in snapshot.blockers
            if (
                (
                    blocker.startswith("PRIOR_POSITION")
                    and not is_exempt_prior_position_blocker(
                        blocker, self.known_dust_residuals
                    )
                    and not self._balance_explained_by_owned(blocker)
                )
                or blocker.startswith("PRIOR_OPEN_ORDER")
            )
        )
        self.capabilities.prior_scope_clear = complete and not prior_exposure_blockers
        for code in tuple(self.capabilities.extra_blockers):
            if code.startswith("ACCOUNT_STATE_"):
                self.capabilities.clear_blocker(code)
        for index, blocker in enumerate(snapshot.blockers):
            if is_exempt_prior_position_blocker(blocker, self.known_dust_residuals):
                continue
            if self._balance_explained_by_owned(blocker):
                continue
            self.capabilities.set_blocker(f"ACCOUNT_STATE_{index}", blocker)
        if snapshot.status in {AccountSnapshotStatus.UNAVAILABLE, AccountSnapshotStatus.STALE}:
            self.capabilities.set_blocker("ACCOUNT_STATE_AUTHORITY", snapshot.status.value)
        else:
            self.capabilities.clear_blocker("ACCOUNT_STATE_AUTHORITY")
        self._record_readiness_if_changed()

    def owned_token_shares(self, token_id: str) -> Decimal:
        """Shares of ``token_id`` this account provably owns through its own records.

        Execution sessions of this process (active, earlier attempts of the window,
        detached committed holds) and the settlement ledger's episodes (fills not sold;
        committed, resolving or settled holdings whose tokens may still be held). Both
        describe the same holdings, so the larger is used, never their sum.
        """
        session_ids = [self.lifecycle.session_id, *self.lifecycle.window_sessions,
                       *self.lifecycle.detached_sessions]
        from_sessions = Decimal(0)
        for session_id in dict.fromkeys(s for s in session_ids if s):
            state = self.coordinator.states.get(session_id)
            if state is None or state.identity is None or state.identity.token_id != token_id:
                continue
            from_sessions += state.confirmed_position_shares
        from_ledger = Decimal(0)
        hooks = self.settlement_hooks
        if hooks is not None:
            from_ledger = hooks.ledger.unsold_shares(token_id)
        return max(from_sessions, from_ledger)

    def _balance_explained_by_owned(self, blocker: str) -> bool:
        """A PRIOR_POSITION balance fully explained by owned positions is not unknown.

        Only the explained quantity is exempt: any excess (a balance above what this
        account's own records explain) keeps the blocker, i.e. UNKNOWN_INVENTORY.
        """
        if not blocker.startswith("PRIOR_POSITION:"):
            return False
        token_id, sep, qty_text = blocker[len("PRIOR_POSITION:"):].rpartition(":")
        if not sep or not token_id:
            return False
        try:
            balance = Decimal(qty_text)
        except Exception:  # noqa: BLE001
            return False
        owned = self.owned_token_shares(token_id)
        return owned > 0 and balance <= owned + self.config.lifecycle.dust_abs_shares

    def readiness_dict(self) -> dict[str, Any]:
        state = self.lifecycle.state
        snap = self.capabilities.snapshot(exposed=bool(state and state.has_exposure))
        return {
            "blockers": [{"code": value} for value in snap.blockers],
            **snap.__dict__,
        }
