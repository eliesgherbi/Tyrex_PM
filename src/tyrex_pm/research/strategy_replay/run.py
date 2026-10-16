"""Closed-loop replay of one recorded window through a real strategy driver.

    recorded rows ──► engine market truth (received_at)
                 └──► strategy view (received_at + information delay)
                        ├─ MarketSessionRuntime / MarketStateStore / IndicatorGraph
                        ├─ EvaluationScheduler (declared triggers, virtual time)
                        └─ driver.evaluate() ──► intents ──► engine orders (+ delays)
                                                   ▲              │
                                                   └── feedback ◄─┘

The strategy never sees engine state directly: it sees a DecisionContext built from
execution feedback that has been delivered by the timeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ids import InstrumentId
from tyrex_pm.core.intents import EnterIntent, HoldToResolutionIntent
from tyrex_pm.core.time_authority import FakeTimeAuthority, TimeSyncStatus
from tyrex_pm.research.replay.config import Intent, Scenario
from tyrex_pm.research.replay.engine import DECISION, STRATEGY_OBSERVATION, TIMER, Engine
from tyrex_pm.runtime.decision_records import (
    decision_context,
    evaluated_record,
    skipped_record,
)
from tyrex_pm.runtime.market_runtime import MarketSessionRuntime
from tyrex_pm.runtime.scheduler import EvaluationScheduler
from tyrex_pm.strategies.context import (
    DecisionContext,
)
from tyrex_pm.strategies.registry import get_strategy_plugin

from .clock_sync import CLOCK_POLICY, RecordedClockAuthority
from .feed import REPLAYED_REFERENCE_SERIES, StrategyFeed, utc
from .labels import PROXY_STATUS, OutcomeLabel, engine_labels, proxy_outcome
from .loader import NS, WindowData

# v2: replay clock corrected from binance_time samples only (clock_sync.CLOCK_POLICY);
# v1 mixed in whole-second clob_time samples and skipped most evaluations.
RUNNER_VERSION = "strategy_closed_loop_v2"
STRATEGY_DATASETS = (
    "book_snapshots",
    "book_updates",
    "operational_events",
    "trade_prints",
    "reference_observations",
    "market_changes",
)


@dataclass
class PositionView:
    """What execution feedback has told the strategy so far."""

    instrument_id: str | None = None
    net_shares: Decimal = Decimal("0")
    cost_total: Decimal = Decimal("0")
    pending_order_ids: set[str] = field(default_factory=set)


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
    holds: list[dict] = field(default_factory=list)
    unsupported_intents: list[dict] = field(default_factory=list)
    _decision_at: int | None = None
    _order_serial: int = 0
    evaluate_seconds: float = 0.0
    strategy_datasets: tuple[str, ...] = STRATEGY_DATASETS
    time_authority: RecordedClockAuthority | None = None
    clock_observations: list[dict] = field(default_factory=list)

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
        )

    def _decide(self, now: int) -> None:
        mono_s = now / NS
        self.scheduler.mark_evaluated(mono_s=mono_s)
        self.feed.flush(now)  # the store is read only here: apply pending book state
        ready = self.runtime.prepare_aligned_eval()
        end_ns = self.window.ref.window_end_ns
        if not ready.ok or ready.snapshot is None or ready.binding is None:
            self.decisions.append(skipped_record(ready, at_ns=now, window_end_ns=end_ns))
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
        for intent in result.intents:
            if isinstance(intent, EnterIntent):
                self._order(intent, now)
            elif isinstance(intent, HoldToResolutionIntent):
                self.holds.append(
                    {"at_ns": now, "instrument_id": intent.instrument_id.value, **intent.evidence}
                )
            else:
                self.unsupported_intents.append(
                    {"at_ns": now, "type": type(intent).__name__, "reason": intent.reason_code}
                )

    def _order(self, intent: EnterIntent, now: int) -> None:
        assert self.engine is not None
        token = intent.instrument_id.value
        wh_iid = {v: k for k, v in self.window.token_by_instrument.items()}[token]
        gross = intent.evidence.get("intended_gross_shares")
        if gross is None or intent.max_price is None:
            self.unsupported_intents.append(
                {"at_ns": now, "type": "EnterIntent", "reason": "missing_intended_shares"}
            )
            return
        self._order_serial += 1
        order_id = f"{self.window.ref.slug}:entry:{self._order_serial}"
        order = Intent(
            order_id=order_id,
            at_ns=now,
            instrument_id=wh_iid,
            action="BUY",
            quantity=str(gross),
            limit_price=str(intent.max_price),
            lifetime="FAK",
        )
        arrival = self.engine.schedule_order(order, decided_at_ns=now)
        # Pending from the decision instant: the context blocks re-entry immediately.
        self.position.pending_order_ids.add(order_id)
        self.orders.append(
            {
                "order_id": order_id,
                "decided_at_ns": now,
                "arrival_ns": arrival,
                "instrument_id": wh_iid,
                "side": intent.evidence.get("side"),
                "quantity": str(gross),
                "limit_price": str(intent.max_price),
                "intended_debit": intent.evidence.get("intended_debit"),
                "ev_conservative": intent.evidence.get("ev_conservative"),
                "ev_point": intent.evidence.get("ev_point"),
                "p_conservative": intent.evidence.get("p_conservative"),
                "p_point": intent.evidence.get("p_point"),
            }
        )

    def _feedback(self, payload: dict) -> None:
        order_id = payload["order_id"]
        for entry in payload["entries"]:
            if entry["event"] == "fill" and entry.get("side") == "BUY":
                self.position.instrument_id = entry["instrument_id"]
                self.position.net_shares += Decimal(str(entry["net_quantity"]))
                self.position.cost_total += Decimal(str(entry["quantity"])) * Decimal(
                    str(entry["price"])
                )
        # FAK orders are terminal after arrival: filled, partially filled or cancelled.
        self.position.pending_order_ids.discard(order_id)
        for order in self.orders:
            if order["order_id"] == order_id:
                order["feedback_at_ns"] = self.engine.now if self.engine else None
                order["engine_events"] = [e["event"] for e in payload["entries"]]


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
) -> dict:
    """Replay one window. Returns a JSON-friendly result bundle (Decimals as strings)."""
    if scenario.adapter != "strategy_closed_loop_v1":
        raise ValueError("strategy replay requires a strategy_closed_loop_v1 scenario")
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
    from .results import config_hash, economics, jsonable

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
            "holds": agent.holds,
            "unsupported_intents": agent.unsupported_intents,
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
            "economics": economics(engine_result, agent.orders, label, window),
            "timing": {
                "wall_seconds": wall_seconds,
                "load_seconds": window.load_seconds,
                "evaluate_seconds": agent.evaluate_seconds,
                "evaluations": sum(1 for d in agent.decisions if d.get("action") != "HOST_SKIPPED"),
                "feed_counts": agent.feed.counts,
            },
        }
    )
