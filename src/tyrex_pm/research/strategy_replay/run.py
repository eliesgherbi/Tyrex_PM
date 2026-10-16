"""Closed-loop replay of one recorded window through a real strategy driver.

    recorded rows ──► engine market truth (received_at)
                 └──► strategy view (received_at + information delay)
                        ├─ MarketSessionRuntime / MarketStateStore / IndicatorGraph
                        ├─ EvaluationScheduler (declared triggers, virtual time)
                        └─ driver.evaluate() ──► intents
                               │
                               ▼  live order contract (v3)
          IntentOrderPlanner ──► final gate (dispatch_check, strategy-visible book at
                               │  decision + decision_delay + final_gate_delay)
                               ▼
                        engine order arrival (decision + decision_delay + order_delay)
                               │
          driver.on_entry_outcome / on_exit_outcome ◄── feedback (+ feedback delay)

The strategy never sees engine state directly: it sees a DecisionContext built from
execution feedback that has been delivered by the timeline. Entry orders follow the live
contract: the planner caps the notional, reserves fees and sends an amount-denominated
FAK BUY; the shared ``dispatch_check`` refuses it before arrival when the visible book or
the candidate moved. Exits (managed-exit policy) and an optional protection spec run
through the same exit arbitration as live. ``ReplayExecutionPolicy.legacy_v2()``
reproduces the v2 share-denominated, ungated order contract for attribution.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal
from typing import Any

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
from tyrex_pm.core.intents import (
    EnterIntent,
    ExitIntent,
    FlattenIntent,
    HoldToResolutionIntent,
)
from tyrex_pm.core.time_authority import FakeTimeAuthority, TimeSyncStatus
from tyrex_pm.execution.exit_arbitration import admit_exit, pick_exit
from tyrex_pm.execution.final_gate import (
    DISPATCH_CHECK_VERSION,
    DispatchFacts,
    FinalGatePolicy,
    dispatch_check,
)
from tyrex_pm.execution.outcome_reasons import classify_reason
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.protection import (
    ArmedProtection,
    ProtectionPhase,
    build_protection_intent,
    mark_triggered,
    observe_mark,
)
from tyrex_pm.research.replay.config import Intent, Scenario
from tyrex_pm.research.replay.engine import DECISION, STRATEGY_OBSERVATION, TIMER, Engine
from tyrex_pm.runtime.decision_records import (
    decision_context,
    evaluated_record,
    skipped_record,
)
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime
from tyrex_pm.runtime.scheduler import EvaluationScheduler
from tyrex_pm.strategies.context import DecisionContext
from tyrex_pm.strategies.outcomes import (
    EntryOutcome,
    EntryOutcomeKind,
    ExitOutcome,
    ExitOutcomeKind,
    RetryClass,
)
from tyrex_pm.strategies.registry import get_strategy_plugin

from .clock_sync import CLOCK_POLICY, RecordedClockAuthority
from .feed import REPLAYED_REFERENCE_SERIES, StrategyFeed, utc
from .labels import PROXY_STATUS, OutcomeLabel, engine_labels, proxy_outcome
from .loader import NS, WindowData

# v3: live order contract (planner, amount-denominated FAK BUY, fee reserve, debit cap),
# shared pre-send dispatch_check at a modelled gate time, execution outcomes fed back to
# the strategy (retry), SELL translation and protection (managed exits), episodes.
# v2: replay clock corrected from binance_time samples only (clock_sync.CLOCK_POLICY).
# v1 mixed in whole-second clob_time samples and skipped most evaluations.
RUNNER_VERSION = "strategy_closed_loop_v3"
EXECUTION_CONTRACT_VERSION = "replay_execution_contract_v1"
STRATEGY_DATASETS = (
    "book_snapshots",
    "book_updates",
    "operational_events",
    "trade_prints",
    "reference_observations",
    "market_changes",
)
GATE = DECISION  # gate checks read the strategy view, after same-instant observations


@dataclass(frozen=True)
class ReplayExecutionPolicy:
    """Host execution rules applied in replay (mirrors the live run config).

    ``order_contract``: ``planner_amount`` (live: capped notional / (1 + fee reserve)
    spent as an amount BUY up to the limit) or ``strategy_shares`` (v2: the strategy's
    intended gross shares at the limit). ``final_gate``: apply ``dispatch_check`` at the
    gate time. ``maximum_total_debit`` None means the strategy's own debit cap;
    ``entry_cutoff_s`` None means the strategy's ``tau_min_s``.
    """

    order_contract: str = "planner_amount"
    final_gate: bool = True
    fee_reserve_rate: Decimal = Decimal("0.02")
    maximum_total_debit: Decimal | None = None
    max_book_age_ms: int = 2_000
    max_candidate_age_ms: int = 5_000
    minimum_entry_depth_shares: Decimal = Decimal("0")
    entry_cutoff_s: float | None = None
    minimum_exit_price: Decimal = Decimal("0.01")
    sell_size_increment: Decimal = Decimal("0.01")
    protection: Any | None = None  # tyrex_pm.protection.ProtectionSpec

    def __post_init__(self) -> None:
        if self.order_contract not in ("planner_amount", "strategy_shares"):
            raise ValueError("order_contract must be planner_amount or strategy_shares")
        if self.fee_reserve_rate < 0 or self.sell_size_increment <= 0:
            raise ValueError("fee_reserve_rate must be >= 0 and sell_size_increment > 0")

    @classmethod
    def legacy_v2(cls) -> "ReplayExecutionPolicy":
        return cls(order_contract="strategy_shares", final_gate=False)

    def identity(self) -> dict:
        out = asdict(self)
        out["protection"] = None if self.protection is None else _jsonable_spec(self.protection)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in out.items()}


def _jsonable_spec(spec: Any) -> Any:
    from .results import jsonable

    return jsonable(spec)


@dataclass
class PositionView:
    """What execution feedback has told the strategy so far."""

    instrument_id: str | None = None
    net_shares: Decimal = Decimal("0")
    cost_total: Decimal = Decimal("0")
    pending_order_ids: set[str] = field(default_factory=set)
    pending_exit_ids: set[str] = field(default_factory=set)
    last_exit_reason: str | None = None
    hold_committed: bool = False


@dataclass
class ReplayAgent:
    window: WindowData
    scenario: Scenario
    strategy_kind: str
    strategy_config: Any
    clock: FakeClock
    runtime: MarketSessionRuntime
    feed: StrategyFeed
    scheduler: EvaluationScheduler
    engine: Engine | None = None
    horizon_ns: int = 0
    position: PositionView = field(default_factory=PositionView)
    decisions: list[dict] = field(default_factory=list)
    orders: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    holds: list[dict] = field(default_factory=list)
    unsupported_intents: list[dict] = field(default_factory=list)
    dropped_intents: list[dict] = field(default_factory=list)
    protection_events: list[dict] = field(default_factory=list)
    _decision_at: int | None = None
    _order_serial: int = 0
    evaluate_seconds: float = 0.0
    strategy_datasets: tuple[str, ...] = STRATEGY_DATASETS
    time_authority: RecordedClockAuthority | None = None
    clock_observations: list[dict] = field(default_factory=list)
    policy: ReplayExecutionPolicy = field(default_factory=ReplayExecutionPolicy)
    planner: IntentOrderPlanner | None = None
    armed: ArmedProtection | None = None
    _attempt_by_order: dict[str, dict] = field(default_factory=dict)

    # -- timeline hooks ----------------------------------------------------------------

    def start(self, engine: Engine, horizon_ns: int) -> None:
        self.engine = engine
        self.horizon_ns = horizon_ns
        first = self.window.range_start_ns
        engine.schedule(first, TIMER, "timer", None)
        # Clock observations apply from their own receive time; the latest one received
        # before the range is already known at its start.
        prior = [r for r in self.clock_observations if r["received_at"] < first]
        if prior and self.time_authority is not None:
            self._apply_clock(prior[-1])
        for row in self.clock_observations:
            if first <= row["received_at"] <= horizon_ns:
                engine.schedule(row["received_at"], STRATEGY_OBSERVATION, "clock", row)

    def handle(self, kind: str, payload: Any) -> None:
        assert self.engine is not None
        now = self.engine.now
        self.clock.advance_to_epoch_ns(now)
        mono_s = now / NS
        if kind == "strategy_observation":
            dataset, row = payload
            fact = self.feed.apply(dataset, row)
            if fact is not None and self.scheduler.should_evaluate(fact, mono_s=mono_s):
                self._request_decision(now)
        elif kind == "timer":
            if self.scheduler.should_evaluate_timer(mono_s=mono_s):
                self._request_decision(now)
            following = now + self.scenario.timer_interval_ms * 1_000_000
            if following <= self.horizon_ns:
                self.engine.schedule(following, TIMER, "timer", None)
        elif kind == "decision":
            self._decision_at = None
            self._decide(now)
        elif kind == "gate":
            self._gate(payload, now)
        elif kind == "feedback":
            self._feedback(payload)
        elif kind == "clock":
            self._apply_clock(payload)
        else:
            raise ValueError(f"unknown timeline event {kind!r}")

    def _apply_clock(self, row: dict) -> None:
        if self.time_authority is None:
            return
        self.time_authority.apply(row)
        self.feed.clock_offset = self.time_authority.offset

    def _request_decision(self, now: int) -> None:
        # Decisions run after every same-nanosecond observation (lowest priority).
        if self._decision_at == now:
            return
        self._decision_at = now
        assert self.engine is not None
        self.engine.schedule(now, DECISION, "decision", None)

    def _driver(self) -> Any:
        return self.runtime.window_driver()

    # -- evaluation ----------------------------------------------------------------

    def _context(self, ready) -> DecisionContext:
        pos = self.position
        return decision_context(
            ready,
            run_id=f"replay:{self.window.ref.slug}",
            target_notional=self.runtime.driver.target_notional,
            position_quantity=pos.net_shares,
            position_cost_total=pos.cost_total,
            position_instrument=None
            if pos.instrument_id is None
            else InstrumentId(self.feed.window.token_by_instrument[pos.instrument_id]),
            pending=bool(pos.pending_order_ids),
            exit_pending=bool(pos.pending_exit_ids),
            last_exit_reason=pos.last_exit_reason,
        )

    def _decide(self, now: int) -> None:
        mono_s = now / NS
        self.scheduler.mark_evaluated(mono_s=mono_s)
        self.feed.flush(now)  # the store is read only here: apply pending book state
        ready = self.runtime.prepare_aligned_eval()
        end_ns = self.window.ref.window_end_ns
        protective = self._protection_intent(now)
        if not ready.ok or ready.snapshot is None or ready.binding is None:
            self.decisions.append(skipped_record(ready, at_ns=now, window_end_ns=end_ns))
            if protective is not None:
                self._submit_exit(protective, now, protective=True)
            return
        context = self._context(ready)
        features = self.runtime.indicator_graph.snapshot()
        started = time.perf_counter()
        result = ready.binding.evaluate(
            market_snapshot=ready.snapshot,
            causation_id=ready.snapshot.causation_id,
            correlation_id=ready.snapshot.correlation_id,
            trigger="replay",
            context=context,
            features=features,
        )
        self.evaluate_seconds += time.perf_counter() - started
        self.decisions.append(evaluated_record(ready, result, at_ns=now, window_end_ns=end_ns))
        exits = [] if protective is None else [protective]
        for intent in result.intents:
            if isinstance(intent, EnterIntent):
                self._order(intent, now)
            elif isinstance(intent, HoldToResolutionIntent):
                self.position.hold_committed = True
                if self.armed is not None:
                    self.armed.phase = ProtectionPhase.DISARMED
                self.holds.append(
                    {
                        "at_ns": now,
                        "instrument_id": intent.instrument_id.value,
                        "reason": intent.reason_code,
                        **intent.evidence,
                    }
                )
            elif isinstance(intent, (ExitIntent, FlattenIntent)):
                exits.append(intent)
            else:
                self.unsupported_intents.append(
                    {"at_ns": now, "type": type(intent).__name__, "reason": intent.reason_code}
                )
        chosen = pick_exit(exits)
        for intent in exits:
            if intent is chosen:
                self._submit_exit(intent, now, protective=intent is protective)
            else:
                self.dropped_intents.append(
                    {"at_ns": now, "reason_code": intent.reason_code,
                     "dropped": "LOWER_PRECEDENCE_SAME_INSTANT"}
                )
                if intent is not protective:
                    self._driver().on_exit_outcome(
                        ExitOutcome(ExitOutcomeKind.NOT_SENT, intent.intent_id.value,
                                    utc(now), reason="LOWER_PRECEDENCE_SAME_INSTANT")
                    )

    # -- protection (managed exits only) ------------------------------------------------

    def _held_bid(self) -> Decimal | None:
        pos = self.position
        if pos.instrument_id is None:
            return None
        view = self.runtime.capture_active_view()
        if view is None:
            return None
        token = self.feed.window.token_by_instrument[pos.instrument_id]
        for leg in (view.up, view.down):
            if leg.token_id == token:
                return leg.quote.best_bid
        return None

    def _protection_intent(self, now: int) -> ExitIntent | FlattenIntent | None:
        spec = self.policy.protection
        pos = self.position
        if spec is None or pos.net_shares <= 0 or pos.hold_committed:
            return None
        if self.armed is None:
            entry_price = pos.cost_total / pos.net_shares
            mark = self._held_bid()
            token = self.feed.window.token_by_instrument[pos.instrument_id]
            self.armed = ArmedProtection.arm(
                session_id=f"replay:{self.window.ref.slug}",
                strategy_id=StrategyId(self.strategy_kind),
                market_id=MarketId(self.window.ref.market_id),
                instrument_id=InstrumentId(token),
                spec=spec,
                entry_price=entry_price,
                confirmed_quantity=pos.net_shares,
                armed_at=utc(now),
                initial_mark=mark if mark is not None else entry_price,
            )
            self.protection_events.append(
                {"at_ns": now, "event": "armed", "entry_price": str(entry_price),
                 "thresholds": dict(self.armed.thresholds.evidence)}
            )
        armed = self.armed
        if armed.phase is not ProtectionPhase.ARMED or pos.pending_exit_ids:
            return None
        mark = self._held_bid()
        if mark is None:
            return None
        decision = observe_mark(armed, mark=mark)
        if not decision.fired or decision.reason is None:
            return None
        mark_triggered(armed, decision)
        self.protection_events.append(
            {"at_ns": now, "event": "triggered", "reason": decision.reason.value,
             "mark": str(mark)}
        )
        return build_protection_intent(
            armed,
            reason=decision.reason,
            created_at=utc(now),
            correlation_id=CorrelationId(f"replay-protection:{now}"),
            causation_id=None,
            evidence=dict(decision.evidence),
        )

    # -- entries -------------------------------------------------------------------------

    def _wh_instrument(self, token: str) -> str:
        return {v: k for k, v in self.window.token_by_instrument.items()}[token]

    def _order(self, intent: EnterIntent, now: int) -> None:
        assert self.engine is not None
        token = intent.instrument_id.value
        wh_iid = self._wh_instrument(token)
        gross = intent.evidence.get("intended_gross_shares")
        if intent.max_price is None or (
            self.policy.order_contract == "strategy_shares" and gross is None
        ):
            self.unsupported_intents.append(
                {"at_ns": now, "type": "EnterIntent", "reason": "missing_intended_shares"}
            )
            return
        self._order_serial += 1
        order_id = f"{self.window.ref.slug}:entry:{self._order_serial}"
        if self.policy.order_contract == "planner_amount":
            assert self.planner is not None
            spec = self.planner.entry(intent, token_id=token, candidate_monotonic_ns=now)
            quantum = Decimal(self.scenario.amount_buy_spend_quantum)
            spend: Decimal | None = (spec.spend_amount / quantum).to_integral_value(
                rounding=ROUND_DOWN
            ) * quantum
            engine_order = Intent(
                order_id=order_id,
                at_ns=now,
                instrument_id=wh_iid,
                action="BUY",
                spend_amount=str(spend),
                limit_price=str(spec.worst_price),
                lifetime="FAK",
            )
            estimated = spend / spec.worst_price
        else:
            spec = None
            engine_order = Intent(
                order_id=order_id,
                at_ns=now,
                instrument_id=wh_iid,
                action="BUY",
                quantity=str(gross),
                limit_price=str(intent.max_price),
                lifetime="FAK",
            )
            estimated = Decimal(str(gross))
            spend = None
        delays = self.scenario
        gate_at = now + (delays.decision_delay_ms + delays.final_gate_delay_ms) * 1_000_000
        arrival_at = now + (delays.decision_delay_ms + delays.order_delay_ms) * 1_000_000
        # Pending from the decision instant: the context blocks re-entry immediately.
        self.position.pending_order_ids.add(order_id)
        attempt = {
            "role": "ENTRY",
            "order_id": order_id,
            "intent_id": intent.intent_id.value,
            "episode_seq": intent.evidence.get("episode_seq"),
            "attempt_seq": intent.evidence.get("attempt_seq"),
            "decided_at_ns": now,
            "gate_at_ns": gate_at,
            "arrival_ns": None,
            "feedback_at_ns": None,
            "instrument_id": wh_iid,
            "side": intent.evidence.get("side"),
            "requested_notional": str(intent.target_notional),
            "allowed_debit": str(self.planner.policy.maximum_total_debit)
            if self.planner is not None
            else None,
            "spend_amount": None if spend is None else str(spend),
            "estimated_shares": str(estimated),
            "limit_price": str(intent.max_price),
            "intended_debit": intent.evidence.get("intended_debit"),
            "intended_gross_shares": intent.evidence.get("intended_gross_shares"),
            "ev_conservative": intent.evidence.get("ev_conservative"),
            "ev_point": intent.evidence.get("ev_point"),
            "p_conservative": intent.evidence.get("p_conservative"),
            "p_point": intent.evidence.get("p_point"),
            "gate_reason": None,
            "outcome": None,
            "outcome_reason": None,
            "retry_class": None,
            "provenance": "replay_simulated",
            "_engine_order": engine_order,
            "_spec": spec,
            "_arrival_at": arrival_at,
            "_intent": intent,
        }
        self.attempts.append(attempt)
        self._attempt_by_order[order_id] = attempt
        if self.policy.final_gate:
            self.engine.schedule(gate_at, GATE, "gate", order_id)
        else:
            self._send(attempt)

    def _gate(self, order_id: str, now: int) -> None:
        attempt = self._attempt_by_order[order_id]
        self.feed.flush(now)
        view = self.runtime.capture_active_view()
        end_ns = self.window.ref.window_end_ns
        cutoff = self.policy.entry_cutoff_s
        if cutoff is None:
            cutoff = float(getattr(getattr(self.strategy_config, "entry", None), "tau_min_s", 0))
        spec = attempt["_spec"] or attempt.get("_exit_spec")
        facts = DispatchFacts(
            now_monotonic_ns=now,
            active_binding_id=None if view is None else view.binding_id,
            entry_tau_s=(end_ns - now) / NS,
            entry_cutoff_s=cutoff if attempt["role"] == "ENTRY" else None,
        )
        gate_policy = FinalGatePolicy(
            max_book_age_ms=self.policy.max_book_age_ms,
            max_candidate_age_ms=self.policy.max_candidate_age_ms,
            minimum_entry_depth_shares=self.policy.minimum_entry_depth_shares,
        )
        reason = None if spec is None else dispatch_check(spec, view, facts, gate_policy)
        attempt["gate_reason"] = reason
        if reason is None:
            self._send(attempt)
            return
        attempt["outcome"] = "NOT_SENT"
        attempt["outcome_reason"] = reason
        attempt["feedback_at_ns"] = now
        if attempt["role"] == "ENTRY":
            self.position.pending_order_ids.discard(order_id)
            retry_class = classify_reason(reason)
            attempt["retry_class"] = retry_class.value
            self._driver().on_entry_outcome(
                EntryOutcome(
                    EntryOutcomeKind.NOT_SENT,
                    attempt["intent_id"],
                    utc(now),
                    reason=reason,
                    retry_class=retry_class,
                )
            )
        else:
            self.position.pending_exit_ids.discard(order_id)
            self._exit_unsent(attempt, now, reason)

    def _send(self, attempt: dict) -> None:
        assert self.engine is not None
        arrival = max(attempt["_arrival_at"], self.engine.now)
        attempt["arrival_ns"] = arrival
        order: Intent = attempt["_engine_order"]
        self.engine.schedule(arrival, 1, "order", order)
        self.orders.append(
            {
                k: v
                for k, v in attempt.items()
                if not k.startswith("_") and k not in ("gate_reason", "outcome",
                                                       "outcome_reason", "retry_class")
            }
            | {
                # Amount BUYs: the shares the spend buys at the limit (an estimate).
                "quantity": order.quantity if not order.spend_amount else attempt.get(
                    "estimated_shares"
                ),
                "quantity_basis": "estimated_at_limit" if order.spend_amount else "shares",
                "spend_amount": order.spend_amount or None,
                "action": order.action,
                "side": attempt.get("side")
                or ("UP" if order.instrument_id == self.window.up_instrument_id else "DOWN"),
            }
        )

    # -- exits ---------------------------------------------------------------------------

    def _submit_exit(
        self, intent: ExitIntent | FlattenIntent, now: int, *, protective: bool
    ) -> None:
        pos = self.position
        admission = admit_exit(
            in_flight_reason=next(iter(pos.pending_exit_ids), None),
            committed_hold=pos.hold_committed,
        )
        if not admission.admitted or pos.net_shares <= 0 or pos.instrument_id is None:
            self.dropped_intents.append(
                {"at_ns": now, "reason_code": intent.reason_code,
                 "dropped": admission.reason if not admission.admitted else "NO_POSITION"}
            )
            if not protective:
                self._driver().on_exit_outcome(
                    ExitOutcome(ExitOutcomeKind.NOT_SENT, intent.intent_id.value, utc(now),
                                reason=admission.reason)
                )
            return
        increment = self.policy.sell_size_increment
        shares = (pos.net_shares / increment).to_integral_value(rounding=ROUND_DOWN) * increment
        min_price = getattr(intent, "min_price", None) or self.policy.minimum_exit_price
        if shares <= 0:
            self.dropped_intents.append(
                {"at_ns": now, "reason_code": intent.reason_code, "dropped": "DUST_ONLY"}
            )
            return
        assert self.planner is not None
        token = self.feed.window.token_by_instrument[pos.instrument_id]
        spec = self.planner.exit(intent, token_id=token, shares=shares,
                                 candidate_monotonic_ns=now)
        if getattr(intent, "min_price", None) is None:
            from dataclasses import replace

            spec = replace(spec, minimum_price=min_price)
        self._order_serial += 1
        order_id = f"{self.window.ref.slug}:exit:{self._order_serial}"
        engine_order = Intent(
            order_id=order_id,
            at_ns=now,
            instrument_id=pos.instrument_id,
            action="SELL",
            quantity=str(shares),
            limit_price=str(spec.minimum_price),
            lifetime="FAK",
        )
        delays = self.scenario
        gate_at = now + (delays.decision_delay_ms + delays.final_gate_delay_ms) * 1_000_000
        arrival_at = now + (delays.decision_delay_ms + delays.order_delay_ms) * 1_000_000
        pos.pending_exit_ids.add(order_id)
        pos.last_exit_reason = intent.reason_code
        attempt = {
            "role": "EXIT",
            "order_id": order_id,
            "intent_id": intent.intent_id.value,
            "exit_reason": intent.reason_code,
            "episode_seq": (intent.evidence or {}).get("episode_seq"),
            "protective": protective,
            "decided_at_ns": now,
            "gate_at_ns": gate_at,
            "arrival_ns": None,
            "feedback_at_ns": None,
            "instrument_id": pos.instrument_id,
            "shares": str(shares),
            "limit_price": str(spec.minimum_price),
            "gate_reason": None,
            "outcome": None,
            "outcome_reason": None,
            "provenance": "replay_simulated",
            "_engine_order": engine_order,
            "_spec": None,
            "_exit_spec": spec,
            "_arrival_at": arrival_at,
            "_intent": intent,
        }
        self.attempts.append(attempt)
        self._attempt_by_order[order_id] = attempt
        if self.policy.final_gate:
            self.engine.schedule(gate_at, GATE, "gate", order_id)
        else:
            self._send(attempt)

    def _exit_unsent(self, attempt: dict, now: int, reason: str) -> None:
        if attempt.get("protective") and self.armed is not None:
            # A protective exit that could not be sent re-arms for the next mark.
            self.armed.phase = ProtectionPhase.ARMED
        if not attempt.get("protective"):
            self._driver().on_exit_outcome(
                ExitOutcome(ExitOutcomeKind.NOT_SENT, attempt["intent_id"], utc(now),
                            reason=reason, remaining_shares=self.position.net_shares)
            )

    # -- feedback ------------------------------------------------------------------------

    def _feedback(self, payload: dict) -> None:
        order_id = payload["order_id"]
        attempt = self._attempt_by_order.get(order_id)
        now = self.engine.now if self.engine else 0
        bought = cost = sold = proceeds = Decimal(0)
        fills = []
        for entry in payload["entries"]:
            if entry["event"] != "fill":
                continue
            fills.append(entry)
            if entry.get("side") == "BUY":
                self.position.instrument_id = entry["instrument_id"]
                self.position.net_shares += Decimal(str(entry["net_quantity"]))
                paid = Decimal(str(entry["quantity"])) * Decimal(str(entry["price"]))
                self.position.cost_total += paid
                bought += Decimal(str(entry["net_quantity"]))
                cost += paid
            elif entry.get("side") == "SELL":
                quantity = Decimal(str(entry["quantity"]))
                held = self.position.net_shares
                released = Decimal(0) if held <= 0 else self.position.cost_total * quantity / held
                self.position.net_shares -= quantity
                self.position.cost_total -= released
                sold += quantity
                proceeds += quantity * Decimal(str(entry["price"])) - Decimal(str(entry["fee"]))
        # FAK orders are terminal after arrival: filled, partially filled or cancelled.
        self.position.pending_order_ids.discard(order_id)
        self.position.pending_exit_ids.discard(order_id)
        events = [e["event"] for e in payload["entries"]]
        for order in self.orders:
            if order["order_id"] == order_id:
                order["feedback_at_ns"] = now
                order["engine_events"] = events
        if attempt is None:
            return
        attempt["feedback_at_ns"] = now
        rejected = next((e for e in payload["entries"] if e["event"] == "rejected"), None)
        unfilled = any(e["event"] == "unfilled_cancelled" for e in payload["entries"])
        attempt["filled_gross"] = str(sum((Decimal(str(f["quantity"])) for f in fills),
                                          Decimal(0)))
        attempt["fees"] = str(sum((Decimal(str(f["fee"])) for f in fills), Decimal(0)))
        if attempt["role"] == "ENTRY":
            if fills:
                kind = EntryOutcomeKind.PARTIAL if unfilled else EntryOutcomeKind.FILLED
                reason, retry_class = None, RetryClass.NONE
            else:
                kind = EntryOutcomeKind.ZERO_FILL
                reason = rejected["reason"] if rejected is not None else "unfilled_cancelled"
                retry_class = classify_reason(reason)
            attempt.update(outcome=kind.value, outcome_reason=reason,
                           retry_class=retry_class.value, filled_net=str(bought),
                           cost=str(cost))
            self._driver().on_entry_outcome(
                EntryOutcome(kind, attempt["intent_id"], utc(now), reason=reason,
                             retry_class=retry_class, confirmed_shares=bought, cost=cost,
                             posted=True)
            )
            return
        remaining = self.position.net_shares
        increment = self.policy.sell_size_increment
        if sold > 0 and remaining < increment:
            kind_x = ExitOutcomeKind.FLAT
        elif sold > 0:
            kind_x = ExitOutcomeKind.PARTIAL
        else:
            kind_x = ExitOutcomeKind.NO_FILL
        reason = None if sold > 0 else (
            rejected["reason"] if rejected is not None else "unfilled_cancelled"
        )
        attempt.update(outcome=kind_x.value, outcome_reason=reason, sold=str(sold),
                       proceeds_net=str(proceeds), remaining=str(remaining))
        if kind_x is ExitOutcomeKind.FLAT:
            # Residual below the sell increment is classified dust, not a position.
            if remaining > 0:
                attempt["dust_shares"] = str(remaining)
            self.position.net_shares = Decimal(0)
            self.position.cost_total = Decimal(0)
            if self.armed is not None:
                self.armed.phase = ProtectionPhase.DISARMED
            self.armed = None
        elif attempt.get("protective") and self.armed is not None:
            self.armed.phase = ProtectionPhase.ARMED
        if not attempt.get("protective"):
            self._driver().on_exit_outcome(
                ExitOutcome(kind_x, attempt["intent_id"], utc(now), reason=reason,
                            remaining_shares=remaining, sold_shares=sold,
                            proceeds=proceeds, posted=True)
            )

    def public_attempts(self) -> list[dict]:
        return [{k: v for k, v in a.items() if not k.startswith("_")} for a in self.attempts]


def _initial_constraints(window: WindowData) -> tuple[Decimal | None, Decimal | None]:
    tick = minimum = None
    for row in window.metadata_rows:
        if row.get("tick_size") is not None:
            tick = Decimal(str(row["tick_size"]))
        if row.get("minimum_order_size_value") is not None:
            minimum = Decimal(str(row["minimum_order_size_value"]))
        if row["received_at"] >= window.ref.window_start_ns:
            break
    return tick, minimum


def _check_lifecycle_policy(strategy_config: Any, policy: ReplayExecutionPolicy) -> None:
    exit_cfg = getattr(strategy_config, "exit", None)
    hold = exit_cfg is None or getattr(exit_cfg, "policy", "hold_to_resolution") == (
        "hold_to_resolution"
    )
    if hold and policy.protection is not None:
        raise ValueError(
            "a protection spec requires exit.policy: managed_exit; hold_to_resolution "
            "never sells a position before resolution"
        )


def run_window(
    window: WindowData,
    *,
    strategy_config: Any,
    scenario: Scenario,
    strategy_kind: str = "settlement_value",
    maximum_trade_age_ms: float = 2000.0,
    depth_levels: int = 64,
    eager_books: bool = False,
    clock_observations: list[dict] | None = None,
    execution_policy: ReplayExecutionPolicy | None = None,
) -> dict:
    """Replay one window. Returns a JSON-friendly result bundle (Decimals as strings)."""
    if scenario.adapter != "strategy_closed_loop_v1":
        raise ValueError("strategy replay requires a strategy_closed_loop_v1 scenario")
    policy = execution_policy or ReplayExecutionPolicy()
    _check_lifecycle_policy(strategy_config, policy)
    started = time.perf_counter()
    clock = FakeClock(_wall=utc(window.range_start_ns))
    clock.advance_to_epoch_ns(window.range_start_ns)
    plugin = get_strategy_plugin(strategy_kind)
    # With recorded clock observations, corrected time follows them (as live clock sync);
    # without, raw receive time is used and the result says so.
    authority = (
        None
        if clock_observations is None
        else RecordedClockAuthority(
            clock=clock,
            max_uncertainty_ms=int(plugin.max_clock_uncertainty_ms(strategy_config)),
        )
    )
    runtime = MarketSessionRuntime.create(
        clock=clock,
        strategy_kind=strategy_kind,
        strategy_config=strategy_config,
        time_authority=authority
        or FakeTimeAuthority(clock=clock, sync_status=TimeSyncStatus.READY, uncertainty_ms=0),
        require_ssr_price_match=False,
        maximum_trade_age_ms=maximum_trade_age_ms,
        freshness_clock=clock,
    )
    max_levels = getattr(getattr(strategy_config, "sizing", None), "max_levels", None)
    if max_levels is not None and max_levels > depth_levels:
        raise ValueError("depth_levels must cover the strategy's sizing.max_levels")
    feed = StrategyFeed.create(window, runtime, clock, depth_levels=depth_levels)
    feed.eager_books = eager_books
    tick, minimum = _initial_constraints(window)
    feed.seed_constraints(tick, None)
    scheduler = EvaluationScheduler(
        plugin.input_contract,
        derived_ready=lambda fact: fact != "ptb.sealed"
        or (runtime.active is not None and runtime.active.sealed is not None),
    )
    sizing = getattr(strategy_config, "sizing", None)
    cap = policy.maximum_total_debit or getattr(sizing, "max_debit_usdc", None) or Decimal("5")
    planner = IntentOrderPlanner(
        ExecutionRiskPolicy(
            maximum_total_debit=cap,
            fee_reserve_rate=policy.fee_reserve_rate,
            minimum_exit_price=policy.minimum_exit_price,
        )
    )
    agent = ReplayAgent(
        window=window,
        scenario=scenario,
        strategy_kind=strategy_kind,
        strategy_config=strategy_config,
        clock=clock,
        runtime=runtime,
        feed=feed,
        scheduler=scheduler,
        time_authority=authority,
        clock_observations=sorted(clock_observations or [], key=lambda r: r["received_at"]),
        policy=policy,
        planner=planner,
    )
    label = proxy_outcome(window)
    engine = Engine(scenario)
    result = engine.run_closed_loop(
        window.events,
        agent,
        engine_labels(window, label),
        accepted_label_statuses=(PROXY_STATUS,),
    )
    return assemble_result(
        window=window,
        scenario=scenario,
        strategy_kind=strategy_kind,
        strategy_config=strategy_config,
        agent=agent,
        engine_result=result,
        label=label,
        constraints={"tick_size": tick, "minimum_order_size_value": minimum,
                     "minimum_order_size_unit": "UNKNOWN"},
        wall_seconds=time.perf_counter() - started,
    )


def assemble_result(
    *,
    window: WindowData,
    scenario: Scenario,
    strategy_kind: str,
    strategy_config: Any,
    agent: ReplayAgent,
    engine_result: dict,
    label: OutcomeLabel,
    constraints: dict,
    wall_seconds: float,
) -> dict:
    from .results import config_hash, economics, execution_source_identity, jsonable

    attempts = agent.public_attempts()
    return jsonable(
        {
            "runner_version": RUNNER_VERSION,
            "strategy_kind": strategy_kind,
            "config_hash": config_hash(strategy_kind, strategy_config),
            "scenario_name": scenario.name or "unnamed",
            "scenario": engine_result["scenario"],
            "window": {
                "market_id": window.ref.market_id,
                "slug": window.ref.slug,
                "window_start_ns": window.ref.window_start_ns,
                "window_end_ns": window.ref.window_end_ns,
                "question": window.question,
                "condition_id": window.condition_id,
                "up_instrument_id": window.up_instrument_id,
                "down_instrument_id": window.down_instrument_id,
                "rules_text": window.rules_text,
                "resolution_source": window.resolution_source,
                "crypto_market_config": window.crypto_market_config,
            },
            "provenance": {
                "source_fingerprint": window.source_fingerprint,
                "range_start_ns": window.range_start_ns,
                "range_end_ns": window.range_end_ns,
                "row_counts": window.counts,
                "replayed_reference_series": REPLAYED_REFERENCE_SERIES,
                "constraints_at_start": constraints,
                "published_depth_levels": agent.feed.depth_levels,
                "clock_correction": "none_raw_receive_time"
                if agent.time_authority is None
                else {
                    "source": "recorded_clock_observations",
                    "policy": CLOCK_POLICY,
                    "observations_applied": agent.time_authority.observations_applied,
                    "rejected_sources": dict(agent.time_authority.rejected_sources),
                    "last_offset_ms": agent.time_authority.estimated_offset_ms,
                    "last_uncertainty_ms": agent.time_authority.uncertainty_ms,
                },
                "maximum_trade_age_ms": agent.runtime.indicator_graph.max_trade_age_ms,
                "execution_policy": agent.policy.identity(),
                "execution_contract": {
                    "version": EXECUTION_CONTRACT_VERSION,
                    "dispatch_check": DISPATCH_CHECK_VERSION,
                    "source": execution_source_identity(),
                },
                "fills_are_simulated": True,
            },
            "label": {
                "label_type": label.label_type,
                "winner": label.winner,
                "reason": label.reason,
                "method": label.method,
                "payout_vector": list(label.payout_vector),
                "payout_unit": label.payout_unit,
                "resolved_at_ns": label.resolved_at_ns,
                "available_at_ns": label.available_at_ns,
                "transaction_hash": label.transaction_hash,
            },
            "seals": agent.feed.seals,
            "decisions": agent.decisions,
            "orders": agent.orders,
            "attempts": attempts,
            "holds": agent.holds,
            "unsupported_intents": agent.unsupported_intents,
            "dropped_intents": agent.dropped_intents,
            "protection_events": agent.protection_events,
            "engine": {
                k: engine_result[k]
                for k in (
                    "cash",
                    "inventory",
                    "payout_receivables",
                    "unrealized_mark_value",
                    "unmarked_instruments",
                    "realized_pnl",
                    "fees_paid",
                    "net_equity_change",
                    "qualified",
                    "limitations",
                    "fee_provenance",
                    "ledger",
                )
            },
            "economics": economics(engine_result, attempts, label, window),
            "timing": {
                "wall_seconds": wall_seconds,
                "load_seconds": window.load_seconds,
                "evaluate_seconds": agent.evaluate_seconds,
                "evaluations": sum(1 for d in agent.decisions if d.get("action") != "HOST_SKIPPED"),
                "feed_counts": agent.feed.counts,
            },
        }
    )


__all__ = [
    "EXECUTION_CONTRACT_VERSION",
    "RUNNER_VERSION",
    "ReplayExecutionPolicy",
    "run_window",
]
