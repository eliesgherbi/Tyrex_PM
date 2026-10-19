"""`settlement repair-attempt-ownership`: verified, append-only, idempotent correction."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from live_harness import ScriptedVenue, entry, make_runtime

from tyrex_pm.application.ownership_repair import RepairRefused, run_repair
from tyrex_pm.execution.evidence import ReconciliationObserved, new_envelope
from tyrex_pm.execution.settlement.ledger import EpisodeState, SettlementLedger
from tyrex_pm.persistence.execution_journal import SqliteExecutionJournal

S = EpisodeState
RETRY_PROFILE = {"retry": {"enabled": True, "max_attempts_per_episode": 3,
                           "max_attempts_per_window": 3,
                           "max_prepare_failures_per_episode": 3,
                           "max_posts_per_episode": 1, "max_posts_per_window": 1}}


async def _pre_fix_window(tmp_path: Path):
    """Never-sent attempt 1, filled attempt 2, and the pre-fix false attribution."""
    venue = ScriptedVenue(["fill"])
    runtime, _driver = await make_runtime(tmp_path, venue, strategy=RETRY_PROFILE,
                                          run_mode="continuous")
    calls = {"n": 0}

    async def gate(_spec):
        calls["n"] += 1
        return (False, "EWMA_NOT_READY") if calls["n"] == 1 else (True, None)

    runtime.coordinator.final_gate = gate
    await runtime._consume_entry(entry(runtime), None)
    attempt_1 = runtime.lifecycle.window_sessions[-1]
    await runtime._consume_entry(entry(runtime, attempt=2), None)
    await runtime.coordinator.apply(ReconciliationObserved(
        **new_envelope(session_id=attempt_1, dedupe_key="reconcile:10:10::True:0"),
        source="readonly_rest", complete=True, confirmed_position_shares=Decimal("10"),
        sellable_shares=Decimal("10"), open_order_ids=()))
    config = runtime.config
    await runtime.coordinator.close()
    runtime._release_state_lock()
    return config, attempt_1


def _journal_events(config, session_id):
    return list(SqliteExecutionJournal(config.state_directory / "execution.sqlite3")
                .load(session_id))


@pytest.mark.asyncio
async def test_repair_verifies_then_corrects_once(tmp_path: Path) -> None:
    config, attempt_1 = await _pre_fix_window(tmp_path)
    before = len(_journal_events(config, attempt_1))
    dry = await run_repair(config, "w1", apply=False, connect_chain=False)
    assert dry["before"]["never_sent"]["confirmed_position_shares"] == "10"
    assert len(_journal_events(config, attempt_1)) == before  # dry run wrote nothing

    first = await run_repair(config, "w1", apply=True, connect_chain=False)
    assert first["after"]["never_sent"]["confirmed_position_shares"] == "0"
    assert first["after"]["never_sent"]["safe_to_open_entry"] is True
    assert any("payout not proven yet" in a for a in first["actions"])
    events = _journal_events(config, attempt_1)
    assert len(events) == before + 1  # appended, the false observation is preserved
    assert any("supersedes_event:" in n for n in events[-1].notes)

    # The established resolution workflow proves a zero payout (simulated here).
    ledger = SettlementLedger(config.settlement.ledger_path)
    ep_id = ledger.by_window("w1").episode_id
    ledger.record("PAYOUT_PROVEN", ep_id, payout_per_share="0", source="ctf")
    ledger.record("EPISODE_COMPLETED", ep_id, reason="zero_payout")
    second = await run_repair(config, "w1", apply=True, connect_chain=False)
    assert second["after"]["filled"]["operational_disposition"] == "SETTLEMENT_ONLY"
    assert second["after"]["filled"]["confirmed_position_shares"] == "0"
    assert second["after"]["filled"]["safe_to_open_entry"] is True
    assert second["after"]["episode"]["state"] == "COMPLETED"

    filled = [s for s in SqliteExecutionJournal(config.state_directory / "execution.sqlite3")
              .sessions() if s.endswith(":e1a2")][0]
    count = len(_journal_events(config, filled))
    third = await run_repair(config, "w1", apply=True, connect_chain=False)
    assert len(_journal_events(config, filled)) == count  # idempotent
    assert any("already recorded" in a for a in third["actions"])
    assert len(SettlementLedger(config.settlement.ledger_path).episodes[ep_id].history) == len(
        ledger.episodes[ep_id].history)


@pytest.mark.asyncio
async def test_repair_refuses_a_window_that_does_not_match(tmp_path: Path) -> None:
    config, _attempt_1 = await _pre_fix_window(tmp_path)
    with pytest.raises(RepairRefused):
        await run_repair(config, "w-unknown", apply=True, connect_chain=False)
