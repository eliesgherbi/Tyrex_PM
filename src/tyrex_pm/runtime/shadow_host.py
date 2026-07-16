"""R5 shadow host: observe path + OMS + portfolio + lifecycle + persistence."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tyrex_pm.adapters.binance.fixture_source import BinanceFixtureSource
from tyrex_pm.adapters.polymarket.discovery import load_market_from_fixture
from tyrex_pm.adapters.polymarket.fixture_source import PolymarketFixtureSource
from tyrex_pm.core.clock import Clock, FakeClock, SystemClock
from tyrex_pm.core.commands import (
    CancelOrderCommand,
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
from tyrex_pm.persistence.snapshot import PersistenceError, StateSnapshotStore
from tyrex_pm.planning.exit_planner import ExitPlanner
from tyrex_pm.planning.plan import PlanStatus
from tyrex_pm.portfolio.portfolio import Portfolio
from tyrex_pm.risk.context import BookReadiness, PortfolioRiskView, RiskConfigView, RiskContext
from tyrex_pm.runtime.config import ObserveConfig, SourceMode
from tyrex_pm.runtime.observe_host import ObserveHost, ObserveRunResult
from tyrex_pm.signals.directional import Direction
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.framework_validation.reference_momentum import (
    ReferenceMomentumStrategy,
)


class ShadowHost(ObserveHost):
    """Extends observe host with shadow execution when ``config.shadow.enable_oms``."""

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
        self.fill_ledger = FillLedger()
        self.portfolio = Portfolio(fill_ledger=self.fill_ledger)
        self.lifecycle = TradeLifecycle(
            order_store=self.order_store, portfolio=self.portfolio
        )
        self.exit_planner = ExitPlanner()
        self.oms: ShadowOMS | None = None
        self._persist: StateSnapshotStore | None = None
        self._recovered = False
        self.commands: list[Any] = []

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
            self.fill_ledger.attach(self.dispatcher)
            self.portfolio.attach(self.dispatcher)
            self.lifecycle.attach(self.dispatcher)
            self.dispatcher.subscribe(
                BookUpdated, self._on_book_updated, priority=50
            )

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
            self.strategy.bump_decision_epoch()
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

    def evaluate_once(self, *, causation_id=None):
        if self.oms is None:
            return super().evaluate_once(causation_id=causation_id)

        snapshot = self.build_snapshot(causation_id=causation_id)
        # Reuse parent signal path by temporarily calling pieces
        self._emit(
            "freshness_assessment",
            {
                "yes": snapshot.yes_freshness.reason_code.value,
                "no": snapshot.no_freshness.reason_code.value,
                "reference": snapshot.reference_freshness.reason_code.value,
                "yes_age_ms": snapshot.yes_freshness.age_ms,
                "no_age_ms": snapshot.no_freshness.age_ms,
                "reference_age_ms": snapshot.reference_freshness.age_ms,
            },
            causation_id=causation_id,
        )
        momentum_value = None
        momentum_ready = False
        momentum_reason = "NO_REFERENCE"
        if snapshot.reference is not None:
            ind = self.momentum.update(snapshot.reference, source_event_id=causation_id)
            momentum_value = ind.value.get("momentum")
            momentum_ready = bool(ind.value.get("ready"))
            momentum_reason = str(ind.value.get("reason"))
            self._emit(
                "indicator_result",
                {
                    "indicator_id": ind.indicator_id,
                    "momentum": None if momentum_value is None else str(momentum_value),
                    "ready": momentum_ready,
                    "reason": momentum_reason,
                },
                causation_id=causation_id,
            )

        from tyrex_pm.signals.directional import build_directional_signal

        signal = build_directional_signal(
            snapshot=snapshot,
            momentum=momentum_value,
            momentum_ready=momentum_ready,
            momentum_reason=momentum_reason,
            threshold=self.config.momentum_threshold,
            max_spread=self.config.max_book_spread,
        )
        self.signals.append(signal)
        self._emit(
            "signal",
            {
                "direction": signal.direction.value,
                "reason_code": signal.reason_code,
                "momentum": None if signal.momentum is None else str(signal.momentum),
            },
            causation_id=signal.causation_id,
            strategy_id=ReferenceMomentumStrategy.STRATEGY_ID,
        )

        assert self.config.risk is not None and self.config.shadow is not None
        life = self.lifecycle.view()
        pos_qty = (
            Decimal("0")
            if life.instrument_id is None
            else self.portfolio.net_quantity(life.instrument_id)
        )
        transition = self.strategy.apply_transition(
            signal,
            DecisionContext(
                run_id=self.run_id,
                mode=self.config.risk.runtime_mode,
                snapshot=snapshot,
                target_notional=self.config.risk.target_notional,
                max_price=self.config.risk.max_price,
                lifecycle=life,
                position_quantity=pos_qty,
                now=self.clock.now_utc(),
                max_hold=self.config.shadow.max_hold,
                flatten_before_close=self.config.shadow.flatten_before_close,
                exit_on_flat=self.config.shadow.exit_on_flat,
                kill_switch_active=self._kill_switch,
            ),
        )
        decision = transition.decision
        self.decisions.append(decision)
        self._emit_observe_decision(decision)
        self._handle_shadow_transition(signal, snapshot, transition)
        return decision

    def _handle_shadow_transition(self, signal, snapshot, transition) -> None:
        if transition.suppressed and not transition.intents:
            if transition.suppress_reason in {
                "REPEATED_DIRECTION",
                "ENTRY_PENDING",
                "ACTIVE_NO_EXIT",
                "ACTIVE_POSITION_BLOCKS_ENTRY",
            }:
                self._emit(
                    "intent_suppressed",
                    {
                        "reason": transition.suppress_reason,
                        "lifecycle": self.lifecycle.state.value,
                        "signal_direction": signal.direction.value,
                    },
                    causation_id=signal.causation_id,
                )
            return

        for intent in transition.intents:
            self.intents.append(intent)
            self._emit(
                "intent_created",
                {
                    "intent_id": intent.intent_id.value,
                    "kind": intent.kind.value,
                    "reason_code": intent.reason_code,
                    "semantic_key": intent.semantic_key(),
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
                self.dedup.forget(intent.semantic_key())
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
                self.dedup.forget(intent.semantic_key())
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
            oid = new_order_id()
            self.lifecycle.note_exit_submitted(oid, when=now)
            self.oms.submit(cmd, order_id=oid)
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
            "fills": self.fill_ledger.snapshot(),
            "portfolio": self.portfolio.snapshot(),
            "lifecycle": self.lifecycle.snapshot(),
            "dedup_keys": self.dedup.snapshot(),
            "kill_switch": self._kill_switch,
            "strategy_epoch": self.strategy.decision_epoch,
            "strategy_last_direction": None
            if self.strategy.last_direction is None
            else self.strategy.last_direction.value,
            "shadow_fee_model": self.config.shadow.fee_model_id,
        }
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
        self.fill_ledger.restore(payload.get("fills") or [])
        self.portfolio.restore(payload.get("portfolio") or {})
        self.lifecycle.restore(payload.get("lifecycle") or {"state": "FLAT"})
        self.dedup.restore(payload.get("dedup_keys") or [])
        self._kill_switch = bool(payload.get("kill_switch", False))
        last_dir = payload.get("strategy_last_direction")
        self.strategy.restore_state(
            decision_epoch=int(payload.get("strategy_epoch") or 0),
            last_direction=None if last_dir is None else Direction(last_dir),
        )
        self._recovered = True
        self._emit(
            "persistence_loaded",
            {"path": str(self._persist.path), "lifecycle": self.lifecycle.state.value},
        )
        self._emit("recovery_complete", {"lifecycle": self.lifecycle.state.value})
        return True

    def run_fixture(self) -> ObserveRunResult:
        if self.oms is None:
            return super().run_fixture()
        if self.config.mode is not SourceMode.FIXTURE:
            raise ValueError("run_fixture requires fixture mode")
        assert self.config.fixture_path is not None
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
                # Fresh run if no/invalid snapshot
                pass
            pm = PolymarketFixtureSource(self.config.fixture_path)
            bn = BinanceFixtureSource(self.config.fixture_path)
            pm.publish_all(
                self.dispatcher,
                correlation_id=self.correlation_id,
                market_id=market.market_id,
            )
            bn.publish_all(
                self.dispatcher,
                correlation_id=self.correlation_id,
                symbol=self.config.binance_symbol,
            )
            self.strategy.on_stop("NORMAL")
            self._maybe_persist()
            self._emit(
                "runtime_stop",
                {
                    "decision_count": len(self.decisions),
                    "intent_count": len(self.intents),
                    "command_count": len(self.commands),
                    "lifecycle": self.lifecycle.state.value,
                    "flat": self.portfolio.is_flat(),
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
