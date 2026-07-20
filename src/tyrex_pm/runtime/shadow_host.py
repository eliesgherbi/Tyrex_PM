"""R5.1 shadow host: single ObserveHost pipeline + OMS/lifecycle/retry hooks.

``ShadowHost`` no longer duplicates the snapshot -> freshness -> indicator ->
signal -> strategy pipeline. It reuses ``ObserveHost.evaluate_once`` and only
overrides the two hooks that differ when ``shadow.enable_oms`` is set:

* ``_build_decision_context`` — adds lifecycle + RetryController gates.
* ``_process_transition`` — routes intents through OMS/portfolio instead of
  the R4 dry path.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.adapters.binance.fixture_source import BinanceFixtureSource
from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture
from tyrex_pm.adapters.polymarket.fixture_source import PolymarketFixtureSource
from tyrex_pm.core.clock import Clock, FakeClock, SystemClock
from tyrex_pm.core.commands import (
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.events import BookUpdated, ReferencePriceUpdated
from tyrex_pm.core.ids import CorrelationId, RunId, new_correlation_id, new_order_id, new_run_id
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.order_store import OrderStore
from tyrex_pm.execution.shadow_oms import ShadowFeeConfig, ShadowFillConfig, ShadowOMS
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState, TradeLifecycle
from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
from tyrex_pm.persistence.snapshot import PersistenceError, StateSnapshotStore
from tyrex_pm.planning.exit_planner import ExitPlanner
from tyrex_pm.planning.plan import PlanStatus
from tyrex_pm.portfolio.portfolio import Portfolio
from tyrex_pm.risk.context import BookReadiness, PortfolioRiskView, RiskConfigView, RiskContext
from tyrex_pm.runtime.config import ObserveConfig, SourceMode
from tyrex_pm.runtime.observe_host import ObserveHost, ObserveRunResult
from tyrex_pm.runtime.retry_controller import ExitRetryPhase, RetryConfig, RetryController
from tyrex_pm.signals.directional import Direction, DirectionalSignal
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ReferenceMomentumStrategy,
)

_EXIT_SETBACK_FROM = frozenset(
    {LifecycleState.ACTIVE, LifecycleState.EXIT_REQUESTED, LifecycleState.EXIT_PENDING}
)


class ShadowHost(ObserveHost):
    """Thin OMS subclass of ``ObserveHost`` — wires OMS/portfolio/lifecycle/retry."""

    def __init__(
        self,
        config: ObserveConfig,
        *,
        clock: Clock | None = None,
        run_id: RunId | None = None,
        correlation_id: CorrelationId | None = None,
    ) -> None:
        super().__init__(
            config, clock=clock, run_id=run_id, correlation_id=correlation_id
        )
        self.order_store = OrderStore()
        self.fills_ledger = FillLedger()
        self.portfolio = Portfolio(fill_ledger=self.fills_ledger)
        self.lifecycle = TradeLifecycle(
            order_store=self.order_store, portfolio=self.portfolio
        )
        self.exit_planner = ExitPlanner()
        self.oms: ShadowOMS | None = None
        self._persist: StateSnapshotStore | None = None
        self._recovered = False
        self.commands: list[Any] = []

        retry_cfg = RetryConfig()
        if config.shadow is not None:
            from datetime import timedelta as _td

            retry_cfg = RetryConfig(
                entry_cooldown=_td(seconds=config.shadow.entry_retry_cooldown_s),
                entry_max_attempts=config.shadow.entry_max_attempts,
                exit_cooldown=_td(seconds=config.shadow.exit_retry_cooldown_s),
                exit_max_normal_retries=config.shadow.exit_max_normal_retries,
                exit_escalate_after=config.shadow.exit_escalate_after,
            )
        self.retry = RetryController(config=retry_cfg)

        if config.shadow is not None and config.shadow.enable_oms:
            fee = ShadowFeeConfig(
                fee_rate=config.shadow.fee_rate,
                model_id=config.shadow.fee_model_id,
            )
            self.oms = ShadowOMS(
                dispatcher=self.dispatcher,
                order_store=self.order_store,
                portfolio=self.portfolio,
                config=ShadowFillConfig(
                    cancel_unfilled_residual=config.shadow.cancel_unfilled_residual,
                    fee=fee,
                ),
            )
            self._persist = StateSnapshotStore(config.shadow.persistence_path)
            self.lifecycle.on_transition(self._on_lifecycle_transition)

    def _attach(self) -> None:
        if self._attached:
            return
        super()._attach()
        if self.oms is not None:
            self.order_store.attach(self.dispatcher)
            self.fills_ledger.attach(self.dispatcher)
            self.portfolio.attach(self.dispatcher)
            self.lifecycle.attach(self.dispatcher)
            self.dispatcher.subscribe(
                BookUpdated, self._on_book_updated, priority=50
            )

    def _start_strategy(self, market) -> None:
        super()._start_strategy(market)
        self.retry.reset_market()

    def _on_book_updated(self, event: BookUpdated) -> None:
        if self.oms is not None:
            self.oms.on_book_updated(event.book)

    def _on_lifecycle_transition(self, prev, new, when) -> None:
        self._emit(
            "lifecycle_transition",
            {"from": prev.value, "to": new.value, "at": when.isoformat()},
        )
        if new is LifecycleState.FLAT and prev is not LifecycleState.FLAT:
            # New entry eligibility after reject/cancel/flat — not signal-direction alone.
            self.binding.bump_decision_epoch()
            self.retry.on_flat()
        elif new is LifecycleState.EXIT_RETRY_WAIT:
            # Single bridge point for every path into EXIT_RETRY_WAIT — host-driven
            # setbacks (plan/risk failure) and order-driven residuals (reject/cancel).
            self.retry.note_exit_plan_failed(reason="LIFECYCLE_EXIT_SETBACK", now=when)
            if self.retry.exit.phase is ExitRetryPhase.MANUAL_INTERVENTION:
                self.lifecycle.note_manual_intervention(when=when)
        self._maybe_persist()

    def invalidate_shadow_books(self, *, reason: str) -> None:
        if self.oms is not None:
            self.oms.invalidate_books()
            self._emit("shadow_books_invalidated", {"reason": reason})

    def _portfolio_view(self, instrument_id: str | None = None) -> PortfolioRiskView:
        assert self.config.shadow is not None
        qty = Decimal("0")
        if instrument_id is not None:
            from tyrex_pm.core.ids import InstrumentId

            qty = self.portfolio.net_quantity(InstrumentId(instrument_id))
        return PortfolioRiskView(
            available=True,
            net_quantity=qty,
            total_cost_notional=self.portfolio.total_notional_at_cost(),
            lifecycle_state=self.lifecycle.state.value,
            has_pending_order=self.order_store.has_pending_entry()
            or self.lifecycle.state
            in {LifecycleState.ENTRY_PENDING, LifecycleState.EXIT_PENDING},
            max_position_notional=self.config.shadow.max_position_notional,
            max_total_exposure=self.config.shadow.max_total_exposure,
        )

    def _build_risk_context(self, snapshot):
        ctx = super()._build_risk_context(snapshot)
        if self.oms is None:
            return ctx
        # Rebuild with portfolio view
        risk_cfg = self.config.risk
        assert risk_cfg is not None
        market = snapshot.market
        yes_state = self.book_store.get(market.yes.instrument_id)
        no_state = self.book_store.get(market.no.instrument_id)
        return RiskContext(
            mode=risk_cfg.runtime_mode,
            now=self.clock.now_utc(),
            market=market,
            snapshot=snapshot,
            yes_quote=snapshot.yes_quote,
            no_quote=snapshot.no_quote,
            yes_book=BookReadiness(
                initialized=yes_state.initialized,
                recovery_required=yes_state.recovery_required,
                tick_size=yes_state.tick_size or market.tick_size,
            ),
            no_book=BookReadiness(
                initialized=no_state.initialized,
                recovery_required=no_state.recovery_required,
                tick_size=no_state.tick_size or market.tick_size,
            ),
            risk_config=RiskConfigView(
                max_notional=risk_cfg.max_notional,
                min_price=risk_cfg.min_price,
                max_price=risk_cfg.max_price,
                max_spread=risk_cfg.max_spread,
                min_liquidity_notional=risk_cfg.min_liquidity_notional,
                no_entry_before_close=risk_cfg.no_entry_before_close,
                kill_switch_active=self._kill_switch,
                config_fingerprint=risk_cfg.fingerprint(),
            ),
            dedup=self.dedup,
            exposure_available=True,
            portfolio=self._portfolio_view(),
        )

    # --- R5.1 hooks (ObserveHost.evaluate_once calls these) ---

    def _book_fingerprint(self, snapshot: DecisionSnapshot, instrument_id) -> str | None:
        """Hash of best bid/ask + touch sizes — used as a retry "material move" gate."""
        if instrument_id is None:
            return None
        market = snapshot.market
        quote = (
            snapshot.yes_quote if instrument_id == market.yes.instrument_id else snapshot.no_quote
        )
        if quote.best_bid is None and quote.best_ask is None:
            return None
        raw = "|".join(
            str(v)
            for v in (
                quote.best_bid,
                quote.best_ask,
                quote.bid_size_at_touch,
                quote.ask_size_at_touch,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _build_decision_context(
        self, snapshot: DecisionSnapshot, signal: DirectionalSignal | None = None
    ) -> DecisionContext | None:
        if self.oms is None:
            return super()._build_decision_context(snapshot, signal)
        assert self.config.risk is not None and self.config.shadow is not None
        life = self.lifecycle.view()
        pos_qty = (
            Decimal("0")
            if life.instrument_id is None
            else self.portfolio.net_quantity(life.instrument_id)
        )
        pos_cost = Decimal("0")
        if life.instrument_id is not None:
            pos = self.portfolio.get(life.instrument_id)
            if pos is not None:
                pos_cost = pos.total_cost
        now = self.clock.now_utc()

        entry_instrument = life.instrument_id
        if signal is not None:
            if signal.direction is Direction.UP:
                entry_instrument = snapshot.market.yes.instrument_id
            elif signal.direction is Direction.DOWN:
                entry_instrument = snapshot.market.no.instrument_id
            if life.state is LifecycleState.FLAT and entry_instrument is not None:
                self.retry.on_directional_transition(signal.direction.value)
        entry_fp = self._book_fingerprint(snapshot, entry_instrument)
        entry_allowed, entry_reason = self.retry.entry_allowed(now=now, book_fingerprint=entry_fp)

        exit_fp = self._book_fingerprint(snapshot, life.instrument_id)
        exit_outstanding = self.retry.exit_outstanding()
        exit_allowed, exit_reason = self.retry.exit_allowed(
            now=now, escalate=self._kill_switch, book_fingerprint=exit_fp
        )
        # A retry attempt that clears cooldown while an exit is still outstanding
        # must bypass the strategy's single-outstanding-exit suppression.
        exit_escalate = exit_allowed and exit_outstanding

        target = self.config.risk.target_notional
        if self.config.z_gap is not None:
            target = self.config.z_gap.target_notional

        unknown = bool(getattr(self, "_unknown_inventory", False))
        return DecisionContext(
            run_id=self.run_id,
            mode=self.config.risk.runtime_mode,
            snapshot=snapshot,
            target_notional=target,
            max_price=self.config.risk.max_price,
            lifecycle=life,
            position_quantity=Decimal("0") if unknown else pos_qty,
            position_cost_total=Decimal("0") if unknown else pos_cost,
            unknown_inventory=unknown,
            now=now,
            max_hold=self.config.shadow.max_hold,
            flatten_before_close=self.config.shadow.flatten_before_close,
            exit_on_flat=self.config.shadow.exit_on_flat,
            kill_switch_active=self._kill_switch,
            entry_allowed=entry_allowed and not unknown,
            entry_block_reason=(
                "UNKNOWN_INVENTORY"
                if unknown
                else (None if entry_allowed else entry_reason)
            ),
            exit_allowed=exit_allowed and not unknown,
            exit_block_reason=(
                "UNKNOWN_INVENTORY"
                if unknown
                else (None if exit_allowed else exit_reason)
            ),
            exit_escalate=exit_escalate,
            exit_urgency=self.retry.exit.urgency,
        )

    def _process_transition(
        self, signal: DirectionalSignal, snapshot: DecisionSnapshot, transition
    ) -> None:
        if self.oms is None:
            super()._process_transition(signal, snapshot, transition)
            return
        self._handle_shadow_transition(signal, snapshot, transition)

    def _process_intents(self, intents, snapshot: DecisionSnapshot) -> None:
        """SHADOW: route economic intents through risk → plan → ShadowOMS."""
        if self.oms is None:
            super()._process_intents(intents, snapshot)
            return
        # Synthetic TransitionResult-compatible path without momentum signal.
        from types import SimpleNamespace

        transition = SimpleNamespace(
            suppressed=False,
            suppress_reason=None,
            intents=list(intents),
        )
        self._handle_shadow_transition(None, snapshot, transition)

    # --- Retry bookkeeping ---

    def _attach_attempt_id(self, intent, snapshot, signal, now):
        """Stamp evidence['attempt_id'] so each scheduled retry gets a unique
        dedup semantic key — no ``dedup.forget`` needed for entry/exit retries.
        """
        if isinstance(intent, EnterIntent):
            fp = self._book_fingerprint(snapshot, intent.instrument_id)
            allowed, _ = self.retry.entry_allowed(now=now, book_fingerprint=fp)
            if not allowed:
                return intent
            if signal is not None:
                direction = signal.direction.value
            else:
                outcome = getattr(intent, "outcome", None)
                direction = outcome.value if outcome is not None else "UNKNOWN"
            # Directional transition bookkeeping (signal may be absent for Z-Gap).
            if self.lifecycle.state is LifecycleState.FLAT:
                self.retry.on_directional_transition(direction)
            rec = self.retry.note_entry_attempt(
                now=now,
                direction=direction,
                book_fingerprint=fp,
                reason=intent.reason_code,
            )
            return replace(intent, evidence={**intent.evidence, "attempt_id": rec.attempt_id})
        if isinstance(intent, (ExitIntent, FlattenIntent)):
            rec = self.retry.note_exit_request(
                now=now,
                reason=intent.reason_code,
                urgency="URGENT" if isinstance(intent, FlattenIntent) else "NORMAL",
                escalate=isinstance(intent, FlattenIntent),
            )
            return replace(intent, evidence={**intent.evidence, "attempt_id": rec.attempt_id})
        return intent

    def _note_intent_denied(self, intent, now: datetime) -> None:
        if isinstance(intent, EnterIntent):
            self.retry.note_entry_risk_denied()
        elif isinstance(intent, (ExitIntent, FlattenIntent)):
            self._note_exit_setback(reason="RISK_DENIED", when=now)

    def _note_exit_setback(self, *, reason: str, when: datetime) -> None:
        """Route an exit/flatten failure into lifecycle + retry bookkeeping.

        Lifecycle transitions are the single source of truth: moving into
        ``EXIT_RETRY_WAIT`` (here, or via order reject/cancel residuals) fires
        ``_on_lifecycle_transition``, which advances ``RetryController`` and
        escalates to ``MANUAL_INTERVENTION`` when retries are exhausted.
        """
        if self.lifecycle.state in _EXIT_SETBACK_FROM:
            self.lifecycle.note_exit_retry_wait(when=when)
        else:
            self.retry.note_exit_plan_failed(reason=reason, now=when)
            if self.retry.exit.phase is ExitRetryPhase.MANUAL_INTERVENTION:
                self.lifecycle.note_manual_intervention(when=when)

    def _handle_shadow_transition(self, signal, snapshot, transition) -> None:
        if transition.suppressed and not transition.intents:
            if transition.suppress_reason in {
                "REPEATED_DIRECTION",
                "ENTRY_PENDING",
                "ACTIVE_NO_EXIT",
                "ACTIVE_POSITION_BLOCKS_ENTRY",
                "EXIT_ALREADY_OUTSTANDING",
                "MANUAL_INTERVENTION",
            }:
                self._emit(
                    "intent_suppressed",
                    {
                        "reason": transition.suppress_reason,
                        "lifecycle": self.lifecycle.state.value,
                        "signal_direction": None
                        if signal is None
                        else signal.direction.value,
                    },
                    causation_id=None if signal is None else signal.causation_id,
                )
            return

        now = self.clock.now_utc()
        for raw_intent in transition.intents:
            intent = self._attach_attempt_id(raw_intent, snapshot, signal, now)
            self.intents.append(intent)
            self._emit(
                "intent_created",
                {
                    "intent_id": intent.intent_id.value,
                    "kind": intent.kind.value,
                    "reason_code": intent.reason_code,
                    "semantic_key": intent.semantic_key(),
                    "observe_only": False,
                    "oms_submit": True,
                    "economics_label": "estimated",
                    "fee_label": "shadow_model",
                },
                causation_id=intent.causation_id,
                strategy_id=getattr(intent, "strategy_id", None),
            )
            inst = getattr(intent, "instrument_id", None)
            risk_ctx = self._build_risk_context(snapshot)
            if inst is not None:
                # Rebuild with instrument-scoped portfolio quantity.
                base = risk_ctx
                risk_ctx = RiskContext(
                    mode=base.mode,
                    now=base.now,
                    market=base.market,
                    snapshot=base.snapshot,
                    yes_quote=base.yes_quote,
                    no_quote=base.no_quote,
                    yes_book=base.yes_book,
                    no_book=base.no_book,
                    risk_config=base.risk_config,
                    dedup=base.dedup,
                    exposure_available=True,
                    portfolio=self._portfolio_view(inst.value),
                )

            decision = self.risk_engine.evaluate(intent, risk_ctx)
            self.risk_decisions.append(decision)
            self._emit(
                "risk_decision",
                {
                    "decision_id": decision.decision_id.value,
                    "approved": decision.approved,
                    "reason_codes": [r.value for r in decision.reason_codes],
                    "intent_kind": intent.kind.value,
                },
                causation_id=decision.causation_id,
            )
            if not decision.approved:
                self._emit(
                    "risk_denied",
                    {
                        "decision_id": decision.decision_id.value,
                        "reason_codes": [r.value for r in decision.reason_codes],
                    },
                    causation_id=decision.causation_id,
                )
                self._note_intent_denied(intent, now)
                continue

            self._emit(
                "risk_approved",
                {"decision_id": decision.decision_id.value},
                causation_id=decision.causation_id,
            )
            self._execute_approved(intent, decision, snapshot)

    def _execute_approved(self, intent, risk_decision, snapshot) -> None:
        assert self.oms is not None
        now = self.clock.now_utc()
        if isinstance(intent, EnterIntent):
            book = (
                snapshot.yes_book
                if intent.instrument_id == snapshot.market.yes.instrument_id
                else snapshot.no_book
            )
            plan_result = self.planner.plan(
                intent,
                risk=risk_decision,
                market=snapshot.market,
                book=book,
                now=now,
                causation_id=intent.causation_id,
            )
            self.plans.append(plan_result)
            if plan_result.status is not PlanStatus.PLANNED or plan_result.plan is None:
                self.retry.note_entry_plan_failed(
                    reason=(
                        "UNKNOWN"
                        if plan_result.fail_reason is None
                        else plan_result.fail_reason.value
                    ),
                    now=now,
                )
                self._emit(
                    "planning_failed",
                    {
                        "intent_id": intent.intent_id.value,
                        "fail_reason": None
                        if plan_result.fail_reason is None
                        else plan_result.fail_reason.value,
                    },
                    causation_id=intent.causation_id,
                )
                return
            plan = plan_result.plan
            self._emit(
                "execution_plan_created",
                {
                    "plan_id": plan.plan_id.value,
                    "side": plan.side.value,
                    "quantity": str(plan.quantity),
                    "limit_price": str(plan.limit_price),
                },
                causation_id=plan.causation_id,
            )
            cmd = SubmitOrderCommand(
                command_id=new_command_id(),
                plan_id=plan.plan_id,
                intent_id=intent.intent_id,
                strategy_id=intent.strategy_id,
                instrument_id=plan.instrument_id,
                market_id=plan.market_id,
                side=plan.side,
                quantity=plan.quantity,
                limit_price=plan.limit_price,
                client_order_id=new_client_order_id(),
                created_at=now,
                correlation_id=intent.correlation_id,
                causation_id=intent.causation_id,
                execution_policy=ExecutionPolicy.NORMAL,
            )
            self.commands.append(cmd)
            self._emit(
                "command_created",
                {"command_id": cmd.command_id.value, "kind": "SUBMIT"},
                causation_id=cmd.causation_id,
            )
            # Lifecycle must be ENTRY_PENDING before fill events from submit.
            oid = new_order_id()
            self.lifecycle.note_entry_submitted(oid, intent.instrument_id, when=now)
            self.retry.note_entry_submitted()
            self.oms.submit(cmd, order_id=oid)
            self._maybe_persist()
            return

        if isinstance(intent, (ExitIntent, FlattenIntent)):
            qty = self.portfolio.net_quantity(intent.instrument_id)
            book = (
                snapshot.yes_book
                if intent.instrument_id == snapshot.market.yes.instrument_id
                else snapshot.no_book
            )
            plan_result = self.exit_planner.plan(
                intent,
                risk=risk_decision,
                market=snapshot.market,
                book=book,
                position_qty=qty,
                now=now,
                causation_id=intent.causation_id,
            )
            self.plans.append(plan_result)
            if plan_result.status is not PlanStatus.PLANNED or plan_result.plan is None:
                self._note_exit_setback(
                    reason=(
                        "UNKNOWN"
                        if plan_result.fail_reason is None
                        else plan_result.fail_reason.value
                    ),
                    when=now,
                )
                self._emit(
                    "planning_failed",
                    {
                        "intent_id": intent.intent_id.value,
                        "fail_reason": None
                        if plan_result.fail_reason is None
                        else plan_result.fail_reason.value,
                    },
                    causation_id=intent.causation_id,
                )
                return
            plan = plan_result.plan
            self._emit(
                "execution_plan_created",
                {
                    "plan_id": plan.plan_id.value,
                    "side": plan.side.value,
                    "quantity": str(plan.quantity),
                    "limit_price": str(plan.limit_price),
                },
                causation_id=plan.causation_id,
            )
            policy = (
                ExecutionPolicy.URGENT
                if isinstance(intent, FlattenIntent)
                else ExecutionPolicy.NORMAL
            )
            cmd = SubmitOrderCommand(
                command_id=new_command_id(),
                plan_id=plan.plan_id,
                intent_id=intent.intent_id,
                strategy_id=intent.strategy_id,
                instrument_id=plan.instrument_id,
                market_id=plan.market_id,
                side=plan.side,
                quantity=plan.quantity,
                limit_price=plan.limit_price,
                client_order_id=new_client_order_id(),
                created_at=now,
                correlation_id=intent.correlation_id,
                causation_id=intent.causation_id,
                execution_policy=policy,
            )
            self.commands.append(cmd)
            self._emit(
                "command_created",
                {"command_id": cmd.command_id.value, "kind": "SUBMIT"},
                causation_id=cmd.causation_id,
            )
            self.lifecycle.note_exit_requested(when=now)
            eid = new_order_id()
            self.lifecycle.note_exit_submitted(eid, when=now)
            self.retry.note_exit_submitted()
            self.oms.submit(cmd, order_id=eid)
            self._maybe_persist()

    def _maybe_persist(self) -> None:
        if self._persist is None or self.config.shadow is None or self.config.risk is None:
            return
        market = self.registry.market
        if market is None:
            return
        payload = {
            "schema_version": 1,
            "run_id": self.run_id.value,
            "market_id": market.market_id.value,
            "runtime_mode": self.config.risk.runtime_mode.value,
            "config_fingerprint": self.config.fingerprint(),
            "updated_at": StateSnapshotStore.now_iso(),
            "orders": self.order_store.snapshot(),
            "fills": self.fills_ledger.snapshot(),
            "portfolio": self.portfolio.snapshot(),
            "lifecycle": self.lifecycle.snapshot(),
            "dedup_keys": self.dedup.snapshot(),
            "retry": self.retry.snapshot(),
            "kill_switch": self._kill_switch,
            "strategy_state": self.binding.persistence_slice(),
            "shadow_fee_model": self.config.shadow.fee_model_id,
        }
        slice_ = payload["strategy_state"]
        payload["strategy_epoch"] = slice_.get("strategy_epoch", 0)
        payload["strategy_last_direction"] = slice_.get("strategy_last_direction")
        self._persist.save(payload)
        self._emit("persistence_saved", {"path": str(self._persist.path)})

    def try_recover(self) -> bool:
        if self._persist is None or self.config.shadow is None or self.config.risk is None:
            return False
        market = self.registry.require_market()
        try:
            payload = self._persist.load(
                expected_market_id=market.market_id.value,
                expected_config_fingerprint=self.config.fingerprint(),
                expected_runtime_mode=self.config.risk.runtime_mode.value,
            )
        except PersistenceError as exc:
            self._emit(
                "persistence_rejected",
                {"error": str(exc)},
            )
            raise
        self.order_store.restore(payload.get("orders") or [])
        self.fills_ledger.restore(payload.get("fills") or [])
        self.portfolio.restore(payload.get("portfolio") or {})
        self.lifecycle.restore(payload.get("lifecycle") or {"state": "FLAT"})
        self.dedup.restore(payload.get("dedup_keys") or [])
        self.retry.restore(payload.get("retry") or {})
        self._kill_switch = bool(payload.get("kill_switch", False))
        strategy_state = dict(payload.get("strategy_state") or {})
        if not strategy_state:
            strategy_state = {
                "strategy_epoch": payload.get("strategy_epoch") or 0,
                "strategy_last_direction": payload.get("strategy_last_direction"),
            }
        self.binding.restore_persistence_slice(strategy_state)
        self._recovered = True
        self._emit(
            "persistence_loaded",
            {"path": str(self._persist.path), "lifecycle": self.lifecycle.state.value},
        )
        self._emit("recovery_complete", {"lifecycle": self.lifecycle.state.value})
        return True

    def mark_unknown_inventory(self, *, active: bool = True) -> None:
        """Test/ops hook: force UNKNOWN inventory gate (no blind sell)."""
        self._unknown_inventory = bool(active)

    def run_fixture(self) -> ObserveRunResult:
        if self.oms is None:
            return super().run_fixture()
        if self.config.mode is not SourceMode.FIXTURE:
            raise ValueError("run_fixture requires fixture mode")
        assert self.config.fixture_path is not None
        self._unknown_inventory = False
        self._attach()
        self._init_flags()
        self._emit(
            "runtime_start",
            {
                "mode": self.config.mode.value,
                "runtime_mode": self._runtime_mode().value,
                "shadow_oms": True,
                "fee_model": self.config.shadow.fee_model_id if self.config.shadow else None,
                "shadow_limitations": [
                    "no_queue_position",
                    "no_latency_model",
                    "no_market_impact",
                    "visible_depth_only",
                ],
                "config_fingerprint": self.config.fingerprint(),
                "strategy_kind": self.config.strategy_kind,
            },
        )
        try:
            market = load_market_from_fixture(self.config.fixture_path)
            self.registry.set_market(market)
            self.portfolio.set_market_id(market.market_id)
            self._start_strategy(market)
            self._emit(
                "market_resolved",
                {
                    "market_id": market.market_id.value,
                    "yes_token": market.yes.token_id.value,
                    "no_token": market.no.token_id.value,
                },
            )
            try:
                self.try_recover()
            except PersistenceError:
                pass
            self._publish_fixture_timeline(market)
            self._run_timer_evaluations()
            self.binding.on_stop("NORMAL")
            self._maybe_persist()
            self._emit(
                "runtime_stop",
                {
                    "decision_count": len(self.decisions),
                    "intent_count": len(self.intents),
                    "command_count": len(self.commands),
                    "lifecycle": self.lifecycle.state.value,
                    "flat": self.portfolio.is_flat(),
                    "estimated_shadow_pnl_label": "estimated_shadow_pnl",
                },
            )
        except Exception as exc:
            self._emit("failure", {"error_type": type(exc).__name__, "message": str(exc)})
            self.sink.flush()
            raise
        finally:
            self.sink.flush()
        return ObserveRunResult(
            run_id=self.run_id,
            correlation_id=self.correlation_id,
            market=self.registry.require_market(),
            decisions=list(self.decisions),
            signals=list(self.signals),
            intents=list(self.intents),
            risk_decisions=list(self.risk_decisions),
            plans=list(self.plans),
            facts_path=self.sink.path,
            fact_count=self.sink.count,
        )
