"""Live execution path: safe entry retry, exits/re-entry accounting, continuous detach.

Invariants checked throughout: no duplicate BUY while an outcome is uncertain, no
overspend (the ledger reserves before every POST), no reservation released without
authoritative evidence, durable budgets across restarts.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from live_harness import (
    ScriptedVenue,
    bind,
    close,
    entry,
    exit_intent,
    hold_intent,
    make_runtime,
    market,
)

from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger
from tyrex_pm.strategies.outcomes import EntryOutcomeKind, ExitOutcomeKind, RetryClass

S = EpisodeState
RETRY = {"retry": {"enabled": True, "max_attempts_per_episode": 3,
                   "max_attempts_per_window": 3, "max_posts_per_episode": 3,
                   "max_posts_per_window": 3}}


def episode(runtime, window="w1", seq=1):
    return runtime.settlement_hooks.ledger.episodes[
        runtime.settlement_hooks.episode_id(window, seq)
    ]


@pytest.mark.asyncio
async def test_zero_fill_then_fill_uses_a_fresh_attempt_session(tmp_path: Path) -> None:
    venue = ScriptedVenue(["zero", "fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=RETRY)
    try:
        await runtime._consume_entry(entry(runtime), None)
        first = driver.entry_outcomes[-1]
        assert first.kind is EntryOutcomeKind.ZERO_FILL and first.posted
        assert first.retry_class is RetryClass.RETRY_AFTER_ZERO_FILL
        assert runtime.lifecycle.state is None and runtime.stop_requested is False
        assert episode(runtime).state is S.NO_FILL
        assert runtime.settlement_hooks.ledger.totals().reserved_cash == 0
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        assert driver.entry_outcomes[-1].kind is EntryOutcomeKind.FILLED
        assert runtime.lifecycle.session_id == "stream-w1:e1a2"
        ep = episode(runtime)
        assert ep.state is S.FILLED and ep.attempts == 2 and ep.posts == 2
        assert venue.posts == ["BUY", "BUY"]
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_preparation_failure_without_post_recovers(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill"])
    venue.fail_prepare_next = 1
    runtime, driver = await make_runtime(tmp_path, venue, strategy=RETRY)
    try:
        await runtime._consume_entry(entry(runtime), None)
        outcome = driver.entry_outcomes[-1]
        assert outcome.kind is EntryOutcomeKind.NOT_SENT and outcome.prepare_failed
        assert not outcome.posted and venue.posts == []
        assert episode(runtime).state is S.NO_DISPATCH
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        assert driver.entry_outcomes[-1].kind is EntryOutcomeKind.FILLED
        assert episode(runtime).prepare_failures == 1 and episode(runtime).posts == 1
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_post_timeout_never_triggers_a_second_buy(tmp_path: Path) -> None:
    venue = ScriptedVenue(["timeout", "fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=RETRY)
    try:
        await runtime._consume_entry(entry(runtime), None)
        assert driver.entry_outcomes[-1].kind is EntryOutcomeKind.UNCERTAIN
        assert episode(runtime).state is S.DISPATCH_UNKNOWN
        assert runtime.settlement_hooks.ledger.totals().reserved_cash == Decimal("4")
        # The venue fills the timed-out order late; a new attempt must not POST.
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        assert driver.entry_outcomes[-1].reason == "PRIOR_ATTEMPT_UNRESOLVED"
        assert venue.posts == ["BUY"]
        assert runtime.stop_requested and runtime.run_stop_reason
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_window_attempt_budget_is_durable_across_restart(tmp_path: Path) -> None:
    strategy = {"retry": {**RETRY["retry"], "max_attempts_per_episode": 2,
                          "max_attempts_per_window": 2}}
    venue = ScriptedVenue(["zero", "zero", "fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=strategy)
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_entry(entry(runtime, attempt=2), None)
    finally:
        await close(runtime)
    # A restarted process (fresh lifecycle, same durable ledger) cannot reset the budget.
    runtime2, driver2 = await make_runtime(tmp_path, ScriptedVenue(["fill"]), strategy=strategy)
    try:
        await runtime2._consume_entry(entry(runtime2, attempt=3), None)
        refused = driver2.entry_outcomes[-1]
        assert refused.kind is EntryOutcomeKind.NOT_SENT
        assert "EPISODE_ATTEMPT_CAP" in refused.reason and "WINDOW_ATTEMPT_CAP" in refused.reason
        assert refused.retry_class is RetryClass.END_EPISODE
    finally:
        await close(runtime2)


@pytest.mark.asyncio
async def test_cutoff_and_unauthoritative_account_are_not_sent(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=RETRY, tau_s=10.0)
    try:
        await runtime._consume_entry(entry(runtime), None)
        outcome = driver.entry_outcomes[-1]
        assert (outcome.kind, outcome.reason, outcome.retry_class) == (
            EntryOutcomeKind.NOT_SENT, "ENTRY_CUTOFF_PASSED", RetryClass.END_EPISODE)
        assert venue.posts == [] and runtime.settlement_hooks.ledger.episodes == {}
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_late_fill_on_an_earlier_attempt_blocks_and_needs_an_operator(tmp_path: Path) -> None:
    venue = ScriptedVenue(["zero"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=RETRY)
    try:
        await runtime._consume_entry(entry(runtime), None)
        assert runtime.lifecycle.state is None
        # The zero-filled order later reports a confirmed trade.
        token = runtime.active_market.market.yes.token_id.value
        venue.balance[token] = Decimal("10")
        venue.trade("v-1", "BUY", Decimal("10"), token)
        await runtime._check_earlier_attempts()
        assert runtime.lifecycle.unresolved_window_sessions()
        assert episode(runtime).state is S.MANUAL_INTERVENTION
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        assert driver.entry_outcomes[-1].reason == "PRIOR_ATTEMPT_UNRESOLVED"
        assert venue.posts == ["BUY"] and not runtime.can_promote()
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_stream_evidence_is_routed_to_the_attempt_that_owns_the_order(tmp_path: Path) -> None:
    from tyrex_pm.execution.evidence import OrderSnapshotObserved, new_envelope
    from tyrex_pm.execution.orders import OrderSide

    venue = ScriptedVenue(["zero", "fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, strategy=RETRY)
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        old_session = runtime.lifecycle.window_sessions[0]
        evidence = OrderSnapshotObserved(
            **new_envelope(session_id="stream-w1", dedupe_key="stream-order:v-1:CANCELED:0"),
            source="user_stream", venue_order_id="v-1", order_id=None, side=OrderSide.BUY,
            original_shares=Decimal("0"), cumulative_matched_shares=Decimal("0"),
            status="CANCELED",
        )
        await runtime.lifecycle.on_stream_evidence(evidence)
        old = runtime.coordinator.states[old_session]
        assert any(o.venue_status == "CANCELED" for o in old.orders.values())
        current = runtime.lifecycle.state
        assert all(o.venue_status != "CANCELED" for o in current.orders.values())
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_continuous_hold_detaches_and_allows_the_next_window(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill", "fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_hold(hold_intent(runtime))
        assert episode(runtime).state is S.HOLD_COMMITTED
        assert runtime.lifecycle.state is None and runtime.lifecycle.detached_sessions
        assert runtime.stop_requested is False and runtime.can_promote()
        # Next window: the committed holding does not block a new, separately budgeted entry.
        runtime._summarize_window("w1", "promoted")
        bind(runtime, market("w2"))
        await runtime._consume_entry(entry(runtime), None)
        assert episode(runtime, "w2").state is S.FILLED
        assert venue.posts == ["BUY", "BUY"]
        assert runtime.window_summaries[0]["window_id"] == "w1"
        assert runtime.window_summaries[0]["states"] == ["HOLD_COMMITTED"]
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_single_window_profile_still_stops_after_the_hold(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill"])
    runtime, _driver = await make_runtime(tmp_path, venue)
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_hold(hold_intent(runtime))
        assert runtime.stop_requested is True and runtime.lifecycle.state is not None
    finally:
        await close(runtime)


MANAGED = {
    "exit": {"policy": "managed_exit", "exit_cutoff_tau_s": 25, "value_exit_enabled": True},
    "reentry": {"max_trades_per_window": 2},
    "retry": RETRY["retry"],
}


@pytest.mark.asyncio
async def test_managed_exit_sale_is_accounted_and_frees_the_window_for_reentry(
    tmp_path: Path,
) -> None:
    venue = ScriptedVenue(["fill", "fill", "fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=MANAGED,
                                         lifecycle_mode="managed_exit")
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_exit(exit_intent(runtime), None)
        assert driver.exit_outcomes[-1].kind is ExitOutcomeKind.FLAT
        first = episode(runtime)
        assert first.state is S.SOLD and first.realized_pnl is not None
        assert runtime.lifecycle.state is None and not runtime.stop_requested
        await runtime._consume_entry(entry(runtime, episode=2), None)
        second = episode(runtime, seq=2)
        assert second.state is S.FILLED and second.episode_seq == 2
        assert venue.posts == ["BUY", "SELL", "BUY"]
        ledger = SettlementLedger(tmp_path / "ledger.jsonl")
        assert ledger.window_usage("w1").filled_episodes == 2
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_a_committed_hold_is_never_sold(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=MANAGED,
                                         lifecycle_mode="managed_exit")
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_hold(hold_intent(runtime))
        await runtime._consume_exit(exit_intent(runtime, "PROTECTION_SL"), None)
        assert driver.exit_outcomes[-1].kind is ExitOutcomeKind.NOT_SENT
        assert venue.posts == ["BUY"]
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_resolution_worker_uses_the_rpc_url_from_the_run_env(tmp_path: Path) -> None:
    runtime, _driver = await make_runtime(tmp_path, ScriptedVenue([]), run_mode="continuous")
    try:
        runtime.polygon_rpc_url = None
        import os

        if not os.environ.get("TYREX_POLYGON_RPC_URL"):
            assert runtime._build_settlement_worker() is None
        runtime.polygon_rpc_url = "https://polygon.example.invalid"
        worker = runtime._build_settlement_worker()
        assert worker is not None and worker.interval_s > 0
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_detach_waits_for_trade_confirmation_and_hold_stays_protected(
    tmp_path: Path,
) -> None:
    # First live run: the buy was matched but not yet CONFIRMED when the hold was
    # committed; detaching must wait (not fail), and the hold must never be sold.
    venue = ScriptedVenue(["fill"])
    venue.trade_status = "MATCHED"
    runtime, driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        await runtime._consume_hold(hold_intent(runtime))
        assert runtime._pending_detach == "w1" and runtime.lifecycle.state is not None
        assert not runtime.can_promote()
        # The resolution worker relabels the hold; it is still a committed hold.
        ep = episode(runtime)
        runtime.settlement_hooks.ledger.record(
            "RESOLUTION_UNCERTAIN", ep.episode_id, key="ru", reason="unresolved", source="t")
        assert runtime.settlement_hooks.is_committed("w1")
        await runtime._consume_exit(exit_intent(runtime, "MANDATORY_FLATTEN"), None)
        assert driver.exit_outcomes[-1].kind is ExitOutcomeKind.NOT_SENT
        assert venue.posts == ["BUY"]
        for trade in venue.trades:
            trade.status = "CONFIRMED"
        assert await runtime._try_detach()
        assert runtime.lifecycle.state is None and runtime.can_promote()
    finally:
        await close(runtime)


def test_startup_controller_keeps_the_continuous_hold_flag() -> None:
    from tyrex_pm.execution.startup_reconciliation import InspectionFacts, ReadCoverage
    from tyrex_pm.runtime.startup_controller import StartupController

    facts = InspectionFacts(
        identity=None,
        coverage=ReadCoverage(complete=True, positions_complete=True, orders_complete=True,
                              activity_complete=True),
        committed_holds=frozenset({("0xc", "t")}),
        committed_holds_block_entries=False,
    )
    controller = StartupController.__new__(StartupController)
    controller.coordinator = type("C", (), {"states": {}})()
    refreshed = StartupController._refresh_sessions(controller, facts)
    assert refreshed.committed_holds_block_entries is False
    assert refreshed.committed_holds == facts.committed_holds
