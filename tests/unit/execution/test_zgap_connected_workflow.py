"""R4 connected z_gap evaluate() → shared execution lifecycle."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from tyrex_pm.core.intents import EnterIntent, ExitIntent, FlattenIntent
from tyrex_pm.execution.coordinator import (
    AccountExecutionCoordinator,
    GatewaySubmissionResult,
    PreparedOrder,
)
from tyrex_pm.execution.evidence import SubmissionFailed
from tyrex_pm.execution.lifecycle import ExecutionLifecycle, LifecyclePolicy
from tyrex_pm.execution.planner import ExecutionRiskPolicy, IntentOrderPlanner
from tyrex_pm.execution.reconciliation import SessionReconciler
from tyrex_pm.execution.session_state import ExecutionPhase
from tyrex_pm.lab.evaluate_replay import evaluation_as_record, replay_configured_run
from tyrex_pm.persistence.execution_journal import MemoryExecutionJournal
from tyrex_pm.runtime.capabilities import CapabilityController
from tyrex_pm.runtime.run_config import load_trading_run_config
from tyrex_pm.strategies.decisions import StrategyAction
from tyrex_pm.strategies.registry import get_strategy_plugin

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
YAML = Path("config/runs/z_gap_tiny_live.yaml")
ENTRY = Path("tests/fixtures/lab/z_gap_entry_eligible.json")
NO_ENTRY = Path("tests/fixtures/lab/z_gap_no_entry_eligible.json")
INVALID = Path("tests/fixtures/lab/z_gap_inputs_invalid.json")
EXIT = Path("tests/fixtures/lab/z_gap_exit_held.json")


class _WorkflowVenue:
    def __init__(self, *, accept: bool = True, ambiguous: bool = False) -> None:
        self.posts: list[str] = []
        self.accept = accept
        self.ambiguous = ambiguous
        self.balance = Decimal("0")
        self.trades: list[SimpleNamespace] = []

    async def prepare_order(self, spec):  # noqa: ANN001
        return PreparedOrder(local_order_id=spec.order_id, digest=spec.order_id, payload=spec)

    async def post_order(self, prepared):  # noqa: ANN001
        if self.ambiguous:
            raise TimeoutError("no http result")
        side = prepared.payload.side.value
        self.posts.append(side)
        if not self.accept:
            return GatewaySubmissionResult(accepted=False, venue_order_id=None, status="rejected")
        if side == "BUY":
            shares = Decimal("10")
            self.balance = shares
        else:
            shares = prepared.payload.shares
            self.balance = Decimal("0")
        venue_id = f"venue-{side.lower()}-{len(self.posts)}"
        trade = SimpleNamespace(
            id=f"trade-{len(self.trades)}",
            taker_order_id=venue_id,
            maker_orders=(),
            status="CONFIRMED",
            side=side,
            size=shares,
            price=Decimal("0.40") if side == "BUY" else Decimal("0.45"),
            asset_id="yes",
            updated_at=NOW,
        )
        self.trades.append(trade)
        return GatewaySubmissionResult(
            accepted=True,
            venue_order_id=venue_id,
            status="matched",
            cumulative_matched_shares=shares,
            trade_ids=(trade.id,),
        )

    async def discard_prepared(self, prepared):  # noqa: ANN001
        del prepared

    async def cancel_orders(self, order_ids):  # noqa: ANN001
        from tyrex_pm.execution.cancel import cancel_result_from_sdk

        return cancel_result_from_sdk({"canceled": tuple(order_ids), "not_canceled": {}})

    async def list_open_orders(self, **_kwargs):
        return ()

    async def list_account_trades(self, **_kwargs):
        return tuple(self.trades)

    async def conditional_balance(self, _token_id):
        return self.balance, Decimal("100")

    async def close(self):
        return None


def _lifecycle(venue: _WorkflowVenue) -> ExecutionLifecycle:
    return ExecutionLifecycle(
        coordinator=AccountExecutionCoordinator(
            journal=MemoryExecutionJournal(),
            gateway=venue,
            final_gate=lambda _spec: asyncio.sleep(0, result=(True, None)),
        ),
        reconciler=SessionReconciler(venue),
        planner=IntentOrderPlanner(ExecutionRiskPolicy(maximum_total_debit=Decimal("5"))),
        capabilities=CapabilityController(
            live_requested=True,
            public_feeds_ready=True,
            active_market_ready=True,
            books_ready=True,
            model_ready=True,
            account_reads_ready=True,
            user_stream_ready=True,
            collateral_ready=True,
            entry_allowance_ready=True,
            order_metadata_ready=True,
            prior_scope_clear=True,
            entry_window_open=True,
            selected_token_sellable=True,
        ),
        policy=LifecyclePolicy(
            evidence_timeout_s=1,
            reconciliation_interval_s=0.001,
            exit_retry_limit=3,
            exit_retry_budget_s=1,
            cancel_delay_s=0.001,
        ),
    )


def test_r4_entry_eligible_pack() -> None:
    results = replay_configured_run(config_path=YAML, fixture_path=ENTRY)
    assert results
    last = results[-1]
    assert last.eligibility.strategy_inputs_eligible is True
    assert last.decision.action is StrategyAction.ENTER
    assert last.intents
    assert isinstance(last.intents[0], EnterIntent)


def test_r4_no_entry_eligible_pack() -> None:
    results = replay_configured_run(config_path=YAML, fixture_path=NO_ENTRY)
    last = results[-1]
    assert last.eligibility.strategy_inputs_eligible is True
    assert last.intents == ()
    assert last.decision.action in {StrategyAction.SKIP, StrategyAction.WAIT}


def test_r4_inputs_invalid_pack() -> None:
    results = replay_configured_run(config_path=YAML, fixture_path=INVALID)
    last = results[-1]
    assert last.eligibility.strategy_inputs_eligible is False
    assert last.eligibility.blockers
    assert not any(isinstance(intent, EnterIntent) for intent in last.intents)


def test_r4_exit_held_pack() -> None:
    results = replay_configured_run(config_path=YAML, fixture_path=EXIT)
    last = results[-1]
    tau_s = last.decision.evidence.get("tau_s")
    assert tau_s is not None and tau_s > 90
    assert last.decision.reason_code == "MARKET_RICH_EXIT"
    assert last.intents
    assert any(isinstance(intent, (ExitIntent, FlattenIntent)) for intent in last.intents)
    assert all(intent.reason_code == "MARKET_RICH_EXIT" for intent in last.intents)
    assert not any(isinstance(intent, EnterIntent) for intent in last.intents)


@pytest.mark.asyncio
async def test_r4_connected_evaluate_to_lifecycle() -> None:
    from tyrex_pm.lab.evaluate_replay import evaluate_replay, load_lab_pack

    run = load_trading_run_config(YAML)
    plugin = get_strategy_plugin(run.strategy_kind)
    entry_pack = load_lab_pack(ENTRY)
    entry_results = evaluate_replay(
        plugin=plugin,
        config=run.strategy,
        steps=entry_pack.steps,
        market=entry_pack.market,
        target_notional=entry_pack.target_notional,
        window_id=entry_pack.window_id,
        ptb=entry_pack.ptb,
    )
    assert entry_results[-1].eligibility.strategy_inputs_eligible is True
    intent = next(
        item for item in entry_results[-1].intents if isinstance(item, EnterIntent)
    )
    venue = _WorkflowVenue()
    lifecycle = _lifecycle(venue)
    await lifecycle.open(
        session_id="session",
        strategy_id="z_gap",
        market_id=entry_pack.market.market_id.value,
        window_id=entry_pack.window_id,
        token_id="yes",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    state = await lifecycle.submit_entry(intent, token_id="yes")
    assert state.phase is ExecutionPhase.POSITION_OPEN
    assert venue.posts == ["BUY"]
    exit_pack = load_lab_pack(EXIT)
    exit_results = evaluate_replay(
        plugin=plugin,
        config=run.strategy,
        steps=exit_pack.steps,
        market=exit_pack.market,
        target_notional=exit_pack.target_notional,
        window_id=exit_pack.window_id,
        ptb=exit_pack.ptb,
        position_quantity=state.confirmed_position_shares,
        instrument_id="yes",
        position_cost_total=state.confirmed_position_shares * Decimal("0.40"),
    )
    exit_eval = exit_results[-1]
    tau_s = exit_eval.decision.evidence.get("tau_s")
    assert tau_s is not None and tau_s > 90
    assert exit_eval.decision.reason_code == "MARKET_RICH_EXIT"
    exit_intent = next(
        item
        for item in exit_eval.intents
        if isinstance(item, (ExitIntent, FlattenIntent))
    )
    assert exit_intent.reason_code == "MARKET_RICH_EXIT"
    state = await lifecycle.submit_exit(
        exit_intent,
        token_id="yes",
        min_order_size=Decimal("0.01"),
        last_bid=Decimal("0.45"),
    )
    assert state.phase in {
        ExecutionPhase.COMPLETED_FLAT,
        ExecutionPhase.COMPLETED_FLAT_WITH_DUST,
    }
    assert venue.posts == ["BUY", "SELL"]
    await lifecycle.coordinator.close()


@pytest.mark.asyncio
async def test_zl1_unresolved_submission_is_not_proven_no_fill() -> None:
    venue = _WorkflowVenue(ambiguous=True)
    lifecycle = _lifecycle(venue)
    await lifecycle.open(
        session_id="session",
        strategy_id="z_gap",
        market_id="lab-zgap",
        window_id="lab-zgap",
        token_id="yes",
        baseline_position_shares=Decimal("0"),
        baseline_sellable_shares=Decimal("0"),
    )
    from tyrex_pm.core.ids import CorrelationId, InstrumentId, MarketId, StrategyId
    from tyrex_pm.core.instruments import OutcomeSide
    from tyrex_pm.core.intents import new_intent_id

    intent = EnterIntent(
        intent_id=new_intent_id(),
        strategy_id=StrategyId("z_gap"),
        instrument_id=InstrumentId("yes"),
        market_id=MarketId("lab-zgap"),
        created_at=NOW,
        correlation_id=CorrelationId("zl1"),
        causation_id=None,
        reason_code="ENTRY_CANDIDATE",
        target_notional=Decimal("5"),
        outcome=OutcomeSide.YES,
        max_price=Decimal("0.51"),
    )
    state = await lifecycle.submit_entry(intent, token_id="yes")
    assert state.phase is not ExecutionPhase.COMPLETED_NO_FILL
    unresolved = state.has_unresolved_mutation_attempt
    assert unresolved or state.phase is ExecutionPhase.MANUAL_INTERVENTION
    events = lifecycle.coordinator.journal.load("session")
    assert any(isinstance(event, SubmissionFailed) for event in events)
    await lifecycle.coordinator.close()


def test_lab_eval_records_do_not_stub_eligibility() -> None:
    replayed = replay_configured_run(config_path=YAML, fixture_path=ENTRY)
    records = [evaluation_as_record(item) for item in replayed]
    assert records[-1]["strategy_inputs_eligible"] is True
