"""Generic N6 Scope A live host — strategy-independent composition.

Wires Risk → Planner → LiveOMS → confirmed fills → Portfolio/TradeLifecycle
→ post-trade reconciliation. Mutations require both ``LiveConfig.mutations_enabled``
and an explicit ``MutationAuthorization`` with ``transport_kind=fake``.

Z-Gap strategies must not import this module's venue types; hosts may.
Does not import ``runtime.r7*``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from tyrex_pm.core.clock import Clock, FakeClock
from tyrex_pm.core.commands import (
    CancelOrderCommand,
    ExecutionPolicy,
    SubmitOrderCommand,
    new_client_order_id,
    new_command_id,
)
from tyrex_pm.core.ids import (
    CorrelationId,
    InstrumentId,
    MarketId,
    OrderId,
    StrategyId,
    new_correlation_id,
    new_order_id,
)
from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent, IntentId, OrderSide
from tyrex_pm.core.modes import RuntimeMode
from tyrex_pm.core.snapshots import BookSnapshot
from tyrex_pm.domain.polymarket.market import BinaryMarket
from tyrex_pm.engine.dispatcher import EventDispatcher
from tyrex_pm.execution.fill_ledger import FillLedger
from tyrex_pm.execution.lineage import (
    LineageRegistry,
    SubmissionAttemptState,
    SubmissionLineage,
    new_submission_attempt_id,
    request_fingerprint,
)
from tyrex_pm.execution.order_store import OrderStatus, OrderStore
from tyrex_pm.execution.polymarket.fake_transport import FakeTransport
from tyrex_pm.execution.polymarket.live_oms import LiveOMS, SubmissionState
from tyrex_pm.execution.polymarket.normalize import fill_events_from_trade
from tyrex_pm.execution.polymarket.readiness import ExecutionReadiness, ReadinessReason
from tyrex_pm.execution.polymarket.reconciliation import ReconciliationService
from tyrex_pm.execution.polymarket.transport import PolymarketTransport, VenueTradeSnapshot
from tyrex_pm.lifecycle.trade_lifecycle import LifecycleState, TradeLifecycle
from tyrex_pm.persistence.snapshot import PersistenceError, StateSnapshotStore
from tyrex_pm.market_data.book_view import BookView
from tyrex_pm.planning.book_revalidation import revalidate_plan_against_book_view
from tyrex_pm.planning.exit_planner import ExitPlanner
from tyrex_pm.planning.plan import PlanStatus
from tyrex_pm.planning.planner import ExecutionPlanner
from tyrex_pm.portfolio.portfolio import Portfolio
from tyrex_pm.risk.context import BookReadiness, PortfolioRiskView, RiskConfigView, RiskContext
from tyrex_pm.risk.dedup import IntentDedupRegistry
from tyrex_pm.risk.engine import RiskEngine
from tyrex_pm.runtime.live_config import LiveConfig, LiveScope
from tyrex_pm.runtime.n6_authorization import MutationAuthorization
from tyrex_pm.runtime.scope_a_ladder import ScopeATimingLadder
from tyrex_pm.reporting.reporter import ReportingPort

FactEmitter = Callable[[str, dict[str, Any]], None]


@dataclass
class N6LiveHost:
    """Generic Scope A host for fake-transport acceptance (and future N7 wiring)."""

    live: LiveConfig
    clock: Clock
    transport: PolymarketTransport
    market: BinaryMarket
    ladder: ScopeATimingLadder
    authorization: MutationAuthorization | None = None
    strategy_id: StrategyId = field(default_factory=lambda: StrategyId("generic"))
    emit_fact: FactEmitter | None = None
    reporter: ReportingPort | None = None
    persistence_path: Path | None = None
    min_valid_order_notional: Decimal = Decimal("1")

    dispatcher: EventDispatcher = field(default_factory=EventDispatcher)
    order_store: OrderStore = field(default_factory=OrderStore)
    fills_ledger: FillLedger = field(default_factory=FillLedger)
    portfolio: Portfolio | None = None
    lifecycle: TradeLifecycle | None = None
    oms: LiveOMS | None = None
    recon: ReconciliationService | None = None
    risk_engine: RiskEngine = field(default_factory=RiskEngine)
    planner: ExecutionPlanner = field(default_factory=ExecutionPlanner)
    exit_planner: ExitPlanner = field(default_factory=ExitPlanner)
    lineage: LineageRegistry = field(default_factory=LineageRegistry)
    dedup: IntentDedupRegistry = field(
        default_factory=lambda: IntentDedupRegistry(lifetime=timedelta(hours=1))
    )
    kill_active: bool = False
    unknown_blocks: bool = False
    inventory_unknown: bool = False
    balance_unknown: bool = False
    user_stream_gap: bool = False
    facts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    _persist: StateSnapshotStore | None = None
    _correlation: CorrelationId = field(default_factory=new_correlation_id)
    _fee_label: str = "estimated"
    _pnl_label: str = "confirmed_fills"
    mutations_dispatched: int = 0
    real_venue_mutations: int = 0

    def __post_init__(self) -> None:
        if self.live.scope is not LiveScope.A:
            raise ValueError("N6LiveHost requires live.scope=A")
        if self.portfolio is None:
            self.portfolio = Portfolio(
                fill_ledger=self.fills_ledger, market_id=self.market.market_id
            )
        if self.lifecycle is None:
            self.lifecycle = TradeLifecycle(
                order_store=self.order_store, portfolio=self.portfolio
            )
        self.order_store.attach(self.dispatcher)
        self.fills_ledger.attach(self.dispatcher)
        self.portfolio.attach(self.dispatcher)
        self.lifecycle.attach(self.dispatcher)

        mutations_ok = self._mutations_armed()
        self.oms = LiveOMS(
            transport=self.transport,
            dispatcher=self.dispatcher,
            order_store=self.order_store,
            portfolio=self.portfolio,
            mutations_enabled=mutations_ok,
            emit_fact=self._fact,
            reporter=self.reporter,
            recon=ReconciliationService(
                order_store=self.order_store, portfolio=self.portfolio
            ),
        )
        self.recon = self.oms.recon
        if self.persistence_path is not None:
            self._persist = StateSnapshotStore(self.persistence_path)
        self._fact(
            "n6_host_started",
            {
                "live_scope": "A",
                "mutations_armed": mutations_ok,
                "transport": type(self.transport).__name__,
                "config_fingerprint": self.live.fingerprint(),
            },
        )

    def _fact(self, fact_type: str, payload: dict[str, Any]) -> None:
        self.facts.append((fact_type, payload))
        if self.emit_fact is not None:
            self.emit_fact(fact_type, payload)
        if self.reporter is not None:
            self.reporter.emit_dict(
                event_family="lifecycle",
                event_type=f"host.{fact_type}",
                payload=payload,
                producer="n6_live_host",
                force_critical=fact_type.startswith("n6_entry")
                or fact_type.startswith("n6_exit")
                or "recon" in fact_type
                or fact_type.endswith("_denied"),
            )

    def attach_reporter(self, reporter: ReportingPort) -> None:
        """Attach reporter after construction (OMS already built in __post_init__)."""
        self.reporter = reporter
        if self.oms is not None:
            self.oms.reporter = reporter

    def _mutations_armed(self) -> bool:
        if not self.live.enabled or not self.live.mutations_enabled:
            return False
        if self.authorization is None:
            return False
        if self.authorization.allows_real_venue_mutation:
            return False
        if self.authorization.transport_kind != "fake":
            return False
        if not isinstance(self.transport, FakeTransport):
            return False
        return True

    def _now(self) -> datetime:
        return self.clock.now_utc()

    def assert_no_real_mutation_transport(self) -> None:
        if type(self.transport).__name__ in {
            "SdkMutationTransport",
            "PolymarketMutationTransport",
        }:
            raise RuntimeError("real mutation transport forbidden in N6 acceptance")
        if not isinstance(self.transport, FakeTransport):
            # Allow only FakeTransport for armed mutations
            if self._mutations_armed():
                raise RuntimeError("mutations armed only against FakeTransport")

    # --- readiness / recon -------------------------------------------------

    def preflight_reconcile(self) -> Any:
        assert self.oms is not None
        self.assert_no_real_mutation_transport()
        report = self.oms.run_reconciliation(market_id=self.market.market_id.value)
        if report.blocks_entry or report.requires_manual:
            self.unknown_blocks = True
        self._fact(
            "n6_preflight_recon",
            {
                "blocks_entry": report.blocks_entry,
                "counts": report.counts(),
                "live_scope": "A",
            },
        )
        return report

    def mark_user_stream_gap(self, gap: bool = True) -> None:
        self.user_stream_gap = gap
        assert self.oms is not None
        self.oms.mark_user_stream(ready=not gap)
        if gap:
            self.unknown_blocks = True
            self._fact("n6_user_stream_gap", {"gap": True})

    def set_balance_disagreement(self) -> None:
        self.balance_unknown = True
        self.unknown_blocks = True
        self._fact("n6_balance_disagreement", {"class": "BALANCE_DISAGREEMENT"})

    def set_inventory_disagreement(self) -> None:
        self.inventory_unknown = True
        self.unknown_blocks = True
        self._fact("n6_inventory_disagreement", {"class": "INVENTORY_DISAGREEMENT"})

    def activate_kill(self) -> None:
        self.kill_active = True
        self._fact("n6_kill_activated", {"blocks_new_exposure": True})

    def refuse_scope_b(self, request: str) -> dict[str, Any]:
        payload = {
            "refused": True,
            "request": request,
            "live_scope": "A",
            "reason": "scope_b_unsupported",
        }
        self._fact("n6_scope_b_refused", payload)
        return payload

    # --- planning / submit -------------------------------------------------

    def _risk_context(self, *, book: BookSnapshot) -> RiskContext:
        from datetime import timedelta

        from tyrex_pm.market_data.decision_snapshot import DecisionSnapshot
        from tyrex_pm.market_data.executable import book_quote
        from tyrex_pm.market_data.freshness import (
            FreshnessAssessment,
            FreshnessReason,
            TimestampBasis,
        )
        from tyrex_pm.core.ids import new_correlation_id as _ncid

        assert self.portfolio is not None
        assert self.lifecycle is not None
        now = self._now()
        fresh = FreshnessAssessment(
            is_fresh=True,
            age_ms=10,
            threshold_ms=60_000,
            timestamp_basis=TimestampBasis.EVENT_TIME,
            reason_code=FreshnessReason.FRESH,
            observed_at=now,
        )
        yes_book = book
        # Minimal no book for context completeness
        no_book = BookSnapshot.from_levels(
            instrument_id=self.market.no.instrument_id,
            ts_event=now,
            bids=[("0.48", "100")],
            asks=[("0.52", "100")],
        )
        snap = DecisionSnapshot(
            market=self.market,
            yes_book=yes_book,
            no_book=no_book,
            yes_quote=book_quote(yes_book),
            no_quote=book_quote(no_book),
            reference=None,
            yes_freshness=fresh,
            no_freshness=fresh,
            reference_freshness=fresh,
            observed_at=now,
            correlation_id=_ncid(),
        )
        cap = self.live.hard_collateral_cap or self.live.max_order_notional or Decimal("5")
        cfg = RiskConfigView(
            max_notional=cap,
            min_price=Decimal("0.01"),
            max_price=Decimal("0.99"),
            max_spread=Decimal("0.50"),
            min_liquidity_notional=Decimal("0"),
            no_entry_before_close=self.ladder.last_allowed_entry_before_end,
            kill_switch_active=self.kill_active,
            config_fingerprint=self.live.fingerprint(),
        )
        pos_qty = self.portfolio.net_quantity(self.market.yes.instrument_id)
        portfolio_view = PortfolioRiskView(
            available=not self.inventory_unknown,
            net_quantity=pos_qty,
            total_cost_notional=self.portfolio.total_notional_at_cost(),
            lifecycle_state=self.lifecycle.state.value,
            has_pending_order=self.lifecycle.state
            in {LifecycleState.ENTRY_PENDING, LifecycleState.EXIT_PENDING},
            max_position_notional=cap,
            max_total_exposure=cap,
        )
        return RiskContext(
            # SHADOW mode for policy compatibility; LiveOMS mutations are gated separately.
            mode=RuntimeMode.SHADOW,
            now=now,
            market=self.market,
            snapshot=snap,
            yes_quote=snap.yes_quote,
            no_quote=snap.no_quote,
            yes_book=BookReadiness(
                initialized=True, recovery_required=False, tick_size=Decimal("0.01")
            ),
            no_book=BookReadiness(
                initialized=True, recovery_required=False, tick_size=Decimal("0.01")
            ),
            risk_config=cfg,
            dedup=self.dedup,
            exposure_available=not self.inventory_unknown,
            portfolio=portfolio_view,
            resolution_capability_available=False,
        )

    def _has_ambiguous_lineage(self) -> bool:
        for aids in self.lineage._by_fingerprint.values():
            for aid in aids:
                lin = self.lineage.get(aid)
                if lin is not None and lin.state is SubmissionAttemptState.AMBIGUOUS:
                    return True
        return False

    def readiness_ok_for_entry(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if self.kill_active:
            reasons.append("kill_active")
        if self.unknown_blocks or self.balance_unknown or self.inventory_unknown:
            reasons.append("UNKNOWN")
        if self.user_stream_gap:
            reasons.append("user_stream_gap")
        if not self.ladder.entry_allowed(self._now()):
            reasons.append("late_entry_skipped")
        if self._has_ambiguous_lineage():
            reasons.append("ambiguous_submission")
        return (len(reasons) == 0, reasons)

    def try_enter(
        self,
        intent: EnterIntent,
        *,
        book: BookSnapshot,
        book_view: BookView | None = None,
        book_evidence: dict[str, Any] | None = None,
        active_binding_id: str | None = None,
    ) -> dict[str, Any]:
        if self.reporter is not None and not self.reporter.allows_new_exposure:
            self._fact(
                "n6_entry_skipped",
                {"reasons": ["CRITICAL_AUDIT_FAILURE_BLOCKS_EXPOSURE"]},
            )
            return {
                "status": "SKIP",
                "reasons": ["CRITICAL_AUDIT_FAILURE_BLOCKS_EXPOSURE"],
            }
        ok, reasons = self.readiness_ok_for_entry()
        if not ok:
            self._fact("n6_entry_skipped", {"reasons": reasons})
            return {"status": "SKIP", "reasons": reasons}

        # Hard cap vs min valid order
        cap = self.live.hard_collateral_cap or self.live.max_order_notional
        if cap is not None and self.min_valid_order_notional > cap:
            self._fact(
                "n6_entry_skipped",
                {
                    "reasons": ["min_valid_order_exceeds_hard_cap"],
                    "min_valid": str(self.min_valid_order_notional),
                    "cap": str(cap),
                },
            )
            return {
                "status": "SKIP",
                "reasons": ["min_valid_order_exceeds_hard_cap"],
            }

        ctx = self._risk_context(book=book)
        decision = self.risk_engine.evaluate(intent, ctx)
        if not decision.approved:
            self._fact(
                "n6_entry_risk_denied",
                {"reasons": [r.reason_code.value for r in decision.policy_results if not r.approved]},
            )
            return {"status": "RISK_DENIED", "decision": decision.decision_id.value}

        plan_res = self.planner.plan(
            intent,
            risk=decision,
            market=self.market,
            book=book,
            now=self._now(),
            book_evidence=book_evidence,
        )
        if plan_res.status is not PlanStatus.PLANNED or plan_res.plan is None:
            return {"status": "UNPLANNABLE", "reason": str(plan_res.fail_reason)}

        plan = plan_res.plan
        # BS-9: version change triggers revalidation, not automatic reject.
        if book_view is not None and active_binding_id is not None and book_evidence:
            rev = revalidate_plan_against_book_view(
                plan,
                book_view,
                active_binding_id=active_binding_id,
                fee_available=True,
            )
            self._fact(
                "n6_book_revalidation",
                {
                    "ok": rev.ok,
                    "reason": rev.reason,
                    "audited_version_change": rev.audited_version_change,
                    **rev.evidence,
                },
            )
            if not rev.ok:
                return {
                    "status": "SKIP",
                    "reasons": [rev.reason or "book_revalidation_failed"],
                    "revalidation": rev.evidence,
                }
        # Fee-inclusive notional check (limit * qty as conservative estimate)
        notional = plan.limit_price * plan.quantity
        if cap is not None and notional > cap:
            self._fact(
                "n6_entry_skipped",
                {"reasons": ["fee_inclusive_notional_exceeds_cap"], "notional": str(notional)},
            )
            return {"status": "SKIP", "reasons": ["fee_inclusive_notional_exceeds_cap"]}

        client_oid = new_client_order_id()
        fp = request_fingerprint(
            intent_id=intent.intent_id,
            plan_id=plan.plan_id,
            market_id=self.market.market_id.value,
            instrument_id=plan.instrument_id.value,
            side=OrderSide.BUY.value,
            quantity=plan.quantity,
            limit_price=plan.limit_price,
            client_order_id=client_oid.value,
        )
        if self.lineage.ambiguous_for_fingerprint(fp) or self.lineage.has_dispatched_or_open(fp):
            self._fact(
                "n6_duplicate_entry_blocked",
                {"request_fingerprint": fp, "reason": "lineage_blocks_retry"},
            )
            return {"status": "BLOCKED_DUPLICATE", "fingerprint": fp}

        attempt_id = new_submission_attempt_id()
        lineage = SubmissionLineage(
            intent_id=intent.intent_id,
            plan_id=plan.plan_id,
            request_fingerprint=fp,
            submission_attempt_id=attempt_id,
            state=SubmissionAttemptState.CREATED,
            created_at=self._now(),
        )
        self.lineage.register(lineage)

        cmd = SubmitOrderCommand(
            command_id=new_command_id(),
            plan_id=plan.plan_id,
            intent_id=intent.intent_id,
            strategy_id=self.strategy_id,
            instrument_id=plan.instrument_id,
            market_id=self.market.market_id,
            side=OrderSide.BUY,
            quantity=plan.quantity,
            limit_price=plan.limit_price,
            client_order_id=client_oid,
            created_at=self._now(),
            correlation_id=self._correlation,
            causation_id=None,
            execution_policy=ExecutionPolicy.NORMAL,
        )
        assert self.oms is not None
        assert self.lifecycle is not None
        oid = new_order_id()
        self.lifecycle.note_entry_submitted(oid, plan.instrument_id, when=self._now())
        self.mutations_dispatched += 1
        submitted = self.oms.submit(cmd, order_id=oid)
        track = self.oms._tracking.get(submitted.value)
        state = SubmissionAttemptState.DISPATCHED
        venue_id = None
        if track is not None:
            if track.submission is SubmissionState.UNKNOWN_SUBMISSION:
                state = SubmissionAttemptState.AMBIGUOUS
                self.unknown_blocks = True
            elif track.submission is SubmissionState.REJECTED:
                state = SubmissionAttemptState.REJECTED
            elif track.submission is SubmissionState.VENUE_ACCEPTED:
                state = SubmissionAttemptState.ACKNOWLEDGED
                venue_id = track.venue_order_id
        lineage2 = replace(
            lineage,
            local_order_id=submitted,
            venue_order_id=venue_id,
            state=state,
        )
        self.lineage.update(lineage2)
        self._fact(
            "n6_entry_submitted",
            {
                **lineage2.to_dict(),
                "submitted_limit_price": str(cmd.limit_price),
                "note": "submitted_limit_is_not_execution_truth",
            },
        )
        self._maybe_persist()
        return {
            "status": state.value,
            "order_id": submitted.value,
            "lineage": lineage2.to_dict(),
            "plan_id": plan.plan_id.value,
            "limit_price": str(cmd.limit_price),
        }

    def ingest_confirmed_trade(
        self,
        *,
        order_id: OrderId,
        trade: VenueTradeSnapshot,
        fee_amount: Decimal | None = None,
        fee_confirmed: bool = False,
    ) -> None:
        """Apply a confirmed venue trade as fill truth (never use submitted limit)."""
        rec = self.order_store.get(order_id)
        if rec is None:
            raise ValueError("unknown order")
        cum = rec.filled_quantity + trade.size
        evt = fill_events_from_trade(
            order_id=order_id,
            instrument_id=rec.instrument_id,
            trade=trade,
            correlation_id=self._correlation,
            cumulative_filled=cum,
            order_quantity=rec.quantity,
        )
        if fee_amount is not None:
            # rebuild with fee — normalize sets 0; publish adjusted via replace
            evt = replace(evt, fee_amount=fee_amount)
        if fee_confirmed:
            self._fee_label = "confirmed"
        else:
            self._fee_label = "estimated"
        self.dispatcher.publish(evt)
        self._fact(
            "n6_confirmed_fill",
            {
                "order_id": order_id.value,
                "fill_price": str(trade.price),
                "fill_quantity": str(trade.size),
                "submitted_limit_not_used": True,
                "fee_label": self._fee_label,
                "fee_amount": str(fee_amount or 0),
            },
        )
        self._maybe_persist()

    def try_exit(
        self,
        intent: ExitIntent | FlattenIntent,
        *,
        book: BookSnapshot,
        limit_price: Decimal,
        quantity: Decimal | None = None,
    ) -> dict[str, Any]:
        assert self.portfolio is not None
        assert self.lifecycle is not None
        qty = self.portfolio.net_quantity(intent.instrument_id)
        if qty <= 0:
            return {"status": "SKIP", "reasons": ["no_confirmed_inventory"]}
        # Inventory-bounded: never exceed confirmed residual
        exit_qty = qty if quantity is None else min(quantity, qty)
        if exit_qty <= 0:
            return {"status": "SKIP", "reasons": ["exit_qty_zero"]}

        if self.inventory_unknown:
            self._fact("n6_exit_blocked", {"reason": "UNKNOWN"})
            return {"status": "BLOCKED", "reasons": ["UNKNOWN"]}

        client_oid = new_client_order_id()
        from tyrex_pm.planning.plan import new_plan_id

        plan_id = new_plan_id()
        fp = request_fingerprint(
            intent_id=intent.intent_id,
            plan_id=plan_id,
            market_id=self.market.market_id.value,
            instrument_id=intent.instrument_id.value,
            side=OrderSide.SELL.value,
            quantity=exit_qty,
            limit_price=limit_price,
            client_order_id=client_oid.value,
        )
        attempt_id = new_submission_attempt_id()
        lineage = SubmissionLineage(
            intent_id=intent.intent_id,
            plan_id=plan_id,
            request_fingerprint=fp,
            submission_attempt_id=attempt_id,
            state=SubmissionAttemptState.CREATED,
            created_at=self._now(),
        )
        self.lineage.register(lineage)
        cmd = SubmitOrderCommand(
            command_id=new_command_id(),
            plan_id=plan_id,
            intent_id=intent.intent_id,
            strategy_id=self.strategy_id,
            instrument_id=intent.instrument_id,
            market_id=self.market.market_id,
            side=OrderSide.SELL,
            quantity=exit_qty,
            limit_price=limit_price,
            client_order_id=client_oid,
            created_at=self._now(),
            correlation_id=self._correlation,
            causation_id=None,
            execution_policy=ExecutionPolicy.NORMAL,
        )
        assert self.oms is not None
        oid = new_order_id()
        self.lifecycle.note_exit_submitted(oid, when=self._now())
        self.mutations_dispatched += 1
        submitted = self.oms.submit(cmd, order_id=oid)
        track = self.oms._tracking.get(submitted.value)
        state = SubmissionAttemptState.DISPATCHED
        venue_id = None
        if track is not None:
            if track.submission is SubmissionState.UNKNOWN_SUBMISSION:
                state = SubmissionAttemptState.AMBIGUOUS
                self.unknown_blocks = True
            elif track.submission is SubmissionState.REJECTED:
                state = SubmissionAttemptState.REJECTED
            elif track.submission is SubmissionState.VENUE_ACCEPTED:
                state = SubmissionAttemptState.ACKNOWLEDGED
                venue_id = track.venue_order_id
        lineage2 = replace(
            lineage,
            local_order_id=submitted,
            venue_order_id=venue_id,
            state=state,
        )
        self.lineage.update(lineage2)
        self._fact(
            "n6_exit_submitted",
            {
                **lineage2.to_dict(),
                "exit_qty": str(exit_qty),
                "confirmed_inventory": str(qty),
                "inventory_bounded": True,
            },
        )
        self._maybe_persist()
        return {
            "status": state.value,
            "order_id": submitted.value,
            "exit_qty": str(exit_qty),
            "lineage": lineage2.to_dict(),
        }

    def mandatory_flatten_if_due(self, *, book: BookSnapshot, limit_price: Decimal) -> dict[str, Any] | None:
        if not self.ladder.requires_mandatory_flatten(self._now()):
            return None
        assert self.portfolio is not None
        if self.portfolio.is_flat():
            return {"status": "FLAT"}
        intent = FlattenIntent(
            intent_id=IntentId(f"flatten-{new_submission_attempt_id()[:8]}"),
            strategy_id=self.strategy_id,
            market_id=self.market.market_id,
            instrument_id=self.market.yes.instrument_id,
            created_at=self._now(),
            correlation_id=self._correlation,
            causation_id=None,
            reason_code="mandatory_flatten",
        )
        self._fact("n6_mandatory_flatten", self.ladder.to_dict())
        return self.try_exit(intent, book=book, limit_price=limit_price)

    def resolve_ambiguous_via_recon(self) -> Any:
        assert self.oms is not None
        report = self.oms.run_reconciliation()
        # Update lineage states for recovered acks
        for track in self.oms._tracking.values():
            if track.submission is SubmissionState.VENUE_ACCEPTED:
                for aid, lin in list(self.lineage._by_attempt.items()):
                    if lin.local_order_id == track.order_id and lin.state is SubmissionAttemptState.AMBIGUOUS:
                        self.lineage.update(
                            replace(
                                lin,
                                state=SubmissionAttemptState.RESOLVED,
                                venue_order_id=track.venue_order_id,
                            )
                        )
                        self.unknown_blocks = False
        self._fact("n6_ambiguity_recon", {"counts": report.counts()})
        return report

    def post_trade_reconcile(self) -> dict[str, Any]:
        assert self.oms is not None
        assert self.portfolio is not None
        report = self.oms.run_reconciliation()
        flat = self.portfolio.is_flat()
        residual = {
            iid: str(self.portfolio.net_quantity(InstrumentId(iid)))
            for iid, pos in self.portfolio._positions.items()
            if pos.quantity != 0
        }
        if residual and flat:
            flat = False  # never report FLAT with residual
        pnl = {
            iid: str(pos.realized_pnl)
            for iid, pos in self.portfolio._positions.items()
        }
        fees = {
            iid: str(pos.fees) for iid, pos in self.portfolio._positions.items()
        }
        out = {
            "flat": flat,
            "residual": residual,
            "realized_pnl": pnl,
            "fees": fees,
            "fee_label": self._fee_label,
            "pnl_label": "confirmed_fills_only",
            "recon_blocks_entry": report.blocks_entry,
            "live_scope": "A",
            "real_venue_mutations": self.real_venue_mutations,
            "mutations_dispatched": self.mutations_dispatched,
        }
        self._fact("n6_post_trade_recon", out)
        return out

    # --- persistence -------------------------------------------------------

    def config_fingerprint(self) -> str:
        return self.live.fingerprint()

    def _snapshot_payload(self) -> dict[str, Any]:
        assert self.lifecycle is not None
        return {
            "schema_version": 1,
            "run_id": self._correlation.value,
            "market_id": self.market.market_id.value,
            "runtime_mode": "N6_LIVE_SCOPE_A",
            "config_fingerprint": self.config_fingerprint(),
            "updated_at": StateSnapshotStore.now_iso(),
            "lifecycle": self.lifecycle.state.value,
            "orders": self.order_store.snapshot(),
            "lineage": self.lineage.to_list(),
            "kill_active": self.kill_active,
            "unknown_blocks": self.unknown_blocks,
        }

    def _maybe_persist(self) -> None:
        if self._persist is None:
            return
        self._persist.save(self._snapshot_payload())

    def recover(self) -> dict[str, Any]:
        if self._persist is None:
            raise PersistenceError("no persistence path")
        try:
            payload = self._persist.load(
                expected_market_id=self.market.market_id.value,
                expected_config_fingerprint=self.config_fingerprint(),
                expected_runtime_mode="N6_LIVE_SCOPE_A",
            )
        except PersistenceError as exc:
            self._fact("n6_recover_refused", {"error": str(exc)})
            raise
        self.lineage = LineageRegistry.from_list(list(payload.get("lineage") or []))
        self.kill_active = bool(payload.get("kill_active"))
        self.unknown_blocks = bool(payload.get("unknown_blocks"))
        # Recon before any action
        report = self.preflight_reconcile()
        self._fact(
            "n6_recovered",
            {
                "lifecycle_at_save": payload.get("lifecycle"),
                "recon_blocks": report.blocks_entry,
            },
        )
        return payload

    def status(self) -> dict[str, Any]:
        assert self.lifecycle is not None
        assert self.portfolio is not None
        return {
            "live": self.live.to_dict(),
            "lifecycle": self.lifecycle.state.value,
            "portfolio_flat": self.portfolio.is_flat(),
            "kill_active": self.kill_active,
            "unknown_blocks": self.unknown_blocks,
            "mutations_armed": self._mutations_armed(),
            "real_venue_mutations": self.real_venue_mutations,
            "ladder": self.ladder.to_dict(),
            "lineage_count": len(self.lineage._by_attempt),
        }
