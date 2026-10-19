"""Position ownership and hold commitment (live run 0a5ff92f, window btc-updown-5m-1792266900).

Observed: attempt 1 was refused at the final gate (never sent), attempt 2 bought 19 UP.
An account refresh 0.34 s after the fill flagged the 19 shares as PRIOR_POSITION, so the
strategy saw UNKNOWN_INVENTORY and never committed the hold; the late-evidence watch of
attempt 1 computed balance 19 - baseline 0 and attributed the same shares to it.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from live_harness import ScriptedVenue, bind, close, entry, make_runtime, market

from tyrex_pm.execution.account_state import (
    AccountSnapshotStatus,
    AccountStateSnapshot,
    TokenAccountState,
)
from tyrex_pm.execution.settlement.ledger import EpisodeState
from tyrex_pm.strategies.outcomes import EntryOutcomeKind

S = EpisodeState
RETRY_PROFILE = {"retry": {"enabled": True, "max_attempts_per_episode": 3,
                           "max_attempts_per_window": 3,
                           "max_prepare_failures_per_episode": 3,
                           "max_posts_per_episode": 1, "max_posts_per_window": 1}}


def episode(runtime, window="w1", seq=1):
    hooks = runtime.settlement_hooks
    return hooks.ledger.episodes[hooks.episode_id(window, seq)]


def token(runtime) -> str:
    return runtime.active_market.market.yes.token_id.value


def snapshot_with_balance(runtime, shares: str) -> AccountStateSnapshot:
    tok = token(runtime)
    return AccountStateSnapshot(
        market_id=runtime.active_market.market.market_id.value,
        token_ids=(tok,),
        status=AccountSnapshotStatus.READY,
        observed_at=datetime.now(timezone.utc),
        observed_monotonic_s=time.monotonic(),
        collateral_balance=Decimal("100"),
        collateral_allowance=Decimal("100"),
        tokens=(TokenAccountState(token_id=tok, balance_shares=Decimal(shares),
                                  allowance_shares=Decimal(shares)),),
        open_order_ids=(),
        blockers=(f"PRIOR_POSITION:{tok}:{shares}",),
        read_attempt=1,
    )


async def refuse_once(runtime) -> None:
    """The first final-gate check refuses (as EWMA_NOT_READY did live); later ones pass."""
    calls = {"n": 0}

    async def gate(_spec):
        calls["n"] += 1
        return (False, "EWMA_NOT_READY") if calls["n"] == 1 else (True, None)

    runtime.coordinator.final_gate = gate


@pytest.mark.asyncio
async def test_hold_is_committed_by_the_host_before_any_account_refresh(tmp_path: Path) -> None:
    # Ordering 1: the fill is processed first; the commit no longer waits for the
    # strategy, so a refresh that follows cannot leave the hold uncommitted.
    venue = ScriptedVenue(["fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        assert episode(runtime).state is S.HOLD_COMMITTED
        assert runtime.lifecycle.detached_sessions  # owned by the settlement ledger
        runtime._apply_account_snapshot(snapshot_with_balance(runtime, "10"))
        assert runtime.capabilities.prior_scope_clear  # our own hold, not unknown
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_a_refresh_before_the_fill_is_owned_is_unknown_until_it_is(
    tmp_path: Path,
) -> None:
    # Ordering 2: the balance shows up before the session owns the fill. It is
    # unexplained at that moment (blocked); once owned it is explained (cleared).
    venue = ScriptedVenue(["fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        runtime._apply_account_snapshot(snapshot_with_balance(runtime, "10"))
        assert not runtime.capabilities.prior_scope_clear
        await runtime._consume_entry(entry(runtime), None)
        runtime._apply_account_snapshot(snapshot_with_balance(runtime, "10"))
        assert runtime.capabilities.prior_scope_clear
        assert episode(runtime).state is S.HOLD_COMMITTED
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_unexplained_inventory_still_blocks(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        runtime._apply_account_snapshot(snapshot_with_balance(runtime, "15"))  # 10 owned
        assert not runtime.capabilities.prior_scope_clear
        assert any(str(b).startswith("PRIOR_POSITION")
                   for b in runtime.capabilities.extra_blockers.values())
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_never_sent_attempt_does_not_claim_the_sibling_fill(tmp_path: Path) -> None:
    venue = ScriptedVenue(["fill"])
    runtime, driver = await make_runtime(tmp_path, venue, strategy=RETRY_PROFILE,
                                         run_mode="continuous")
    try:
        await refuse_once(runtime)
        await runtime._consume_entry(entry(runtime), None)
        first = driver.entry_outcomes[-1]
        assert first.kind is EntryOutcomeKind.NOT_SENT and not first.posted
        attempt_1 = runtime.lifecycle.window_sessions[-1]
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        assert driver.entry_outcomes[-1].kind is EntryOutcomeKind.FILLED
        assert venue.posts == ["BUY"]
        # The late-evidence watch reconciles attempt 1 against the shared balance.
        runtime.lifecycle.released_at[attempt_1] = 0.0
        await runtime.lifecycle.reconciler.reconcile(runtime.coordinator, attempt_1)
        state_1 = runtime.coordinator.states[attempt_1]
        assert state_1.confirmed_position_shares == 0
        assert "balance_not_attributable:no_buy_evidence" in state_1.reconciliation_notes
        await runtime._check_earlier_attempts()
        assert not runtime.lifecycle.unresolved_window_sessions()
        assert "PRIOR_ATTEMPT_UNRESOLVED" not in runtime.capabilities.extra_blockers
        assert episode(runtime).state is S.HOLD_COMMITTED
        assert runtime.owned_token_shares(token(runtime)) == Decimal("10")  # once
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_genuine_late_fill_beside_a_filled_sibling_is_still_detected(
    tmp_path: Path,
) -> None:
    # Attempt 1 was POSTed and reported a zero fill; attempt 2 then filled. Later the
    # venue reports attempt 1 filled after all: that is real extra inventory.
    venue = ScriptedVenue(["zero", "fill"])
    runtime, _driver = await make_runtime(
        tmp_path, venue,
        strategy={"retry": {**RETRY_PROFILE["retry"], "max_posts_per_episode": 2,
                            "max_posts_per_window": 2}},
        run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        attempt_1 = runtime.lifecycle.window_sessions[-1]
        await runtime._consume_entry(entry(runtime, attempt=2), None)
        assert venue.posts == ["BUY", "BUY"]
        tok = token(runtime)
        venue.balance[tok] += Decimal("10")
        venue.trade("v-1", "BUY", Decimal("10"), tok)
        await runtime.lifecycle.reconciler.reconcile(runtime.coordinator, attempt_1)
        assert runtime.coordinator.states[attempt_1].confirmed_position_shares == 10
        runtime.lifecycle.released_at[attempt_1] = 0.0
        await runtime._check_earlier_attempts()
        assert runtime.lifecycle.unresolved_window_sessions()  # blocked for an operator
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_committed_hold_survives_rollover_and_shutdown_without_flattening(
    tmp_path: Path,
) -> None:
    from tyrex_pm.execution.session_state import ExecutionPhase

    venue = ScriptedVenue(["fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        held = runtime.lifecycle.detached_sessions[0]
        runtime._summarize_window("w1", "promoted")
        bind(runtime, market("w2"))  # rollover
        assert episode(runtime).state is S.HOLD_COMMITTED
        await runtime._shutdown_execution("stop: max_duration_s")
        assert venue.posts == ["BUY"]  # no SELL, no flatten
        assert episode(runtime).state is S.HOLD_COMMITTED
        state = runtime.coordinator.states[held]
        assert state.phase is not ExecutionPhase.MANUAL_INTERVENTION
        assert not runtime.runtime_errors
    finally:
        await close(runtime)


@pytest.mark.asyncio
async def test_hold_commit_waits_for_unconfirmed_trades_and_retries(tmp_path: Path) -> None:
    # Pending confirmations keep their checks: a matched-but-unconfirmed fill is
    # committed (the ledger records it) but detached only once trades confirm.
    venue = ScriptedVenue(["fill"])
    venue.trade_status = "MATCHED"
    runtime, _driver = await make_runtime(tmp_path, venue, run_mode="continuous")
    try:
        await runtime._consume_entry(entry(runtime), None)
        assert runtime.settlement_hooks.is_committed("w1")
        assert runtime._pending_detach == "w1" and runtime.lifecycle.state is not None
        for trade in venue.trades:
            trade.status = "CONFIRMED"
        assert await runtime._try_detach()
        assert runtime.lifecycle.state is None
    finally:
        await close(runtime)
