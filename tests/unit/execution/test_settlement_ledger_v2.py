"""Settlement ledger schema 2: attempts, early sales, window budgets, run limits."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tyrex_pm.execution.settlement.governor import (
    RunLimits,
    TrialLimits,
    WindowBudget,
    check_entry,
)
from tyrex_pm.execution.settlement.ledger import (
    OPERATOR_SCOPE,
    EpisodeState,
    LedgerError,
    SettlementLedger,
)

S = EpisodeState
TRIAL = TrialLimits(
    max_debit_per_episode=Decimal("5"),
    max_episodes=50,
    max_unresolved_count=5,
    max_unresolved_cost=Decimal("50"),
    trial_loss_budget=Decimal("100"),
)
RUN = RunLimits(
    daily_loss_limit=Decimal("12"),
    max_open_worst_case_loss=Decimal("12"),
    max_reserved_debit=Decimal("10"),
    max_unredeemed_count=2,
    max_unredeemed_cost=Decimal("20"),
    max_consecutive_losses=2,
)


def reserve(ledger: SettlementLedger, ep: str, window: str, **extra) -> None:
    ledger.record("EPISODE_RESERVED", ep, window_id=window, market_id="m", condition_id="0xc",
                  token_id="t", outcome_index=0, max_debit="5", max_fees="0", **extra)


def test_schema1_journal_reads_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    v1 = [
        {"event": "EPISODE_RESERVED", "episode_id": "run:w1", "key": "EPISODE_RESERVED:run:w1",
         "payload": {"window_id": "w1", "market_id": "m", "condition_id": "0xc",
                     "token_id": "t", "outcome_index": 0, "max_debit": "5", "max_fees": "0"},
         "seq": 1, "recorded_at": "2026-10-10T10:00:00+00:00"},
        {"event": "FILL_CONFIRMED", "episode_id": "run:w1", "key": "FILL_CONFIRMED:run:w1",
         "payload": {"net_shares": "10", "cost_basis": "4"}, "seq": 2,
         "recorded_at": "2026-10-10T10:00:01+00:00"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in v1) + "\n", encoding="utf-8")
    ledger = SettlementLedger(path)
    ep = ledger.episodes["run:w1"]
    assert ep.state is S.FILLED and ep.cost_basis == ep.total_cost == Decimal("4")
    assert ep.attempts == 1 and ep.episode_seq == 1
    # The same v1 record replayed by the hooks is a duplicate, not a conflict.
    ledger.record("FILL_CONFIRMED", "run:w1", net_shares="10", cost_basis="4")
    assert len(path.read_text().splitlines()) == 2


def test_retry_rearms_the_same_episode_and_counts_posts(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    reserve(ledger, "e", "w1")
    ledger.record("DISPATCH_NO_FILL", "e", posted=True, attempt=1)
    assert ledger.totals().reserved_cash == 0
    with pytest.raises(LedgerError, match="next attempt"):
        ledger.record("ATTEMPT_REARMED", "e", key="r", attempt=3, max_debit="5", max_fees="0")
    key = SettlementLedger.attempt_key("ATTEMPT_REARMED", "e", 2)
    ledger.record("ATTEMPT_REARMED", "e", key=key, attempt=2, max_debit="5", max_fees="0")
    assert ledger.episodes["e"].state is S.RESERVED and ledger.totals().reserved_cash == 5
    key = SettlementLedger.attempt_key("FILL_CONFIRMED", "e", 2)
    ledger.record("FILL_CONFIRMED", "e", key=key, net_shares="10", cost_basis="3",
                  posted=True, attempt=2)
    usage = ledger.window_usage("w1")
    assert (usage.episodes, usage.attempts, usage.posts, usage.filled_episodes) == (1, 2, 2, 1)
    reopened = SettlementLedger(tmp_path / "l.jsonl")
    assert reopened.window_usage("w1") == usage  # restart-proof counters


def test_early_sales_reduce_basis_and_complete_as_sold(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    reserve(ledger, "e", "w1")
    ledger.record("FILL_CONFIRMED", "e", net_shares="10", cost_basis="4")
    ledger.record("EXIT_FILL_CONFIRMED", "e", key="x1", sold_shares="4", proceeds="1.2")
    ep = ledger.episodes["e"]
    assert ep.net_shares == Decimal("6") and ep.cost_basis == Decimal("2.4")
    # 4 shares cost 1.6 and returned 1.2: 0.4 already lost stays in open risk.
    assert ep.worst_case_open_loss == Decimal("0.4") + Decimal("2.4")
    with pytest.raises(LedgerError, match="exceeds"):
        ledger.record("EXIT_FILL_CONFIRMED", "e", key="x2", sold_shares="7", proceeds="1")
    ledger.record("EXIT_FILL_CONFIRMED", "e", key="x3", sold_shares="6", proceeds="2.4")
    ledger.record("EPISODE_SOLD", "e")
    assert ep.state is S.SOLD and ep.realized_pnl == Decimal("3.6") - Decimal("4")
    totals = ledger.totals()
    assert totals.realized_losses == Decimal("0.4") and totals.open_worst_case_loss == 0


def test_partial_sale_then_resolution_counts_both(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    reserve(ledger, "e", "w1")
    ledger.record("FILL_CONFIRMED", "e", net_shares="10", cost_basis="5")
    ledger.record("EXIT_FILL_CONFIRMED", "e", key="x", sold_shares="5", proceeds="3")
    ledger.record("HOLD_REQUESTED", "e", quantity="5")
    ledger.record("HOLD_COMMITTED", "e", quantity="5")
    ledger.record("PAYOUT_PROVEN", "e", payout_per_share="1")
    ledger.record("REDEMPTION_REQUESTED", "e", mode="operator")
    ledger.record("CASH_CONFIRMED", "e", credited_amount="5")
    ledger.record("EPISODE_COMPLETED", "e", reason="cash_confirmed")
    assert ledger.episodes["e"].realized_pnl == Decimal("3") + Decimal("5") - Decimal("5")


def test_window_cap_allows_a_second_episode_only_after_closure(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    reserve(ledger, "e1", "w1")
    ledger.record("FILL_CONFIRMED", "e1", net_shares="10", cost_basis="4")
    with pytest.raises(LedgerError, match="open episode"):
        reserve(ledger, "e2", "w1", episode_seq=2, window_episode_cap=2)
    ledger.record("EXIT_FILL_CONFIRMED", "e1", key="x", sold_shares="10", proceeds="4.2")
    ledger.record("EPISODE_SOLD", "e1")
    reserve(ledger, "e2", "w1", episode_seq=2, window_episode_cap=2)
    with pytest.raises(LedgerError, match="already has an episode"):
        reserve(ledger, "e3", "w1", episode_seq=3, window_episode_cap=2)
    assert ledger.by_window("w1").episode_id == "e2"


def test_governor_window_budget_is_durable(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    budget = WindowBudget(max_episodes=1, max_attempts=2, max_posts=2,
                          max_attempts_per_episode=2, max_posts_per_episode=2)
    first = check_entry(ledger, TRIAL, window_id="w1", proposed_debit=Decimal("5"),
                        proposed_fees=Decimal(0), window_budget=budget)
    assert first.allowed
    reserve(ledger, "e", "w1")
    ledger.record("DISPATCH_NO_FILL", "e", posted=True, attempt=1)
    second = check_entry(ledger, TRIAL, window_id="w1", proposed_debit=Decimal("5"),
                         proposed_fees=Decimal(0), rearm_episode="e", window_budget=budget)
    assert second.allowed
    ledger.record("ATTEMPT_REARMED", "e", key="a2", attempt=2, max_debit="5", max_fees="0")
    ledger.record("DISPATCH_NO_FILL", "e", key="nf2", posted=True, attempt=2)
    third = check_entry(SettlementLedger(tmp_path / "l.jsonl"), TRIAL, window_id="w1",
                        proposed_debit=Decimal("5"), proposed_fees=Decimal(0),
                        rearm_episode="e", window_budget=budget)
    assert not third.allowed
    assert {"EPISODE_ATTEMPT_CAP", "EPISODE_POST_CAP", "WINDOW_ATTEMPT_CAP",
            "WINDOW_POST_CAP"} <= set(third.reasons)


def _filled(ledger, ep, window, cost="5"):
    reserve(ledger, ep, window)
    ledger.record("FILL_CONFIRMED", ep, net_shares="10", cost_basis=cost)


def test_run_limits_daily_loss_counts_open_risk_and_survives_midnight(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    _filled(ledger, "a", "w1")
    ledger.record("HOLD_REQUESTED", "a", quantity="10")
    ledger.record("HOLD_COMMITTED", "a", quantity="10")
    today = datetime.now(timezone.utc).date()
    check = check_entry(ledger, TRIAL, window_id="w2", proposed_debit=Decimal("5"),
                        proposed_fees=Decimal(0), run_limits=RUN,
                        available_collateral=Decimal("100"), today=today)
    assert check.allowed and check.daily_loss_used_after == Decimal("10")
    # The next UTC day: no realized loss yet, but the open holding is still at risk.
    from datetime import timedelta

    tomorrow = check_entry(ledger, TRIAL, window_id="w2", proposed_debit=Decimal("5"),
                           proposed_fees=Decimal(0), run_limits=RUN,
                           available_collateral=Decimal("100"), today=today + timedelta(days=1))
    assert tomorrow.daily_loss_used_after == Decimal("10")
    _filled(ledger, "b", "w2")
    blocked = check_entry(ledger, TRIAL, window_id="w3", proposed_debit=Decimal("5"),
                          proposed_fees=Decimal(0), run_limits=RUN,
                          available_collateral=Decimal("100"), today=today)
    assert {"DAILY_LOSS_LIMIT", "MAX_OPEN_WORST_CASE_LOSS"} <= set(blocked.reasons)


def test_run_limits_collateral_unredeemed_and_consecutive_losses(tmp_path: Path) -> None:
    ledger = SettlementLedger(tmp_path / "l.jsonl")
    poor = check_entry(ledger, TRIAL, window_id="w1", proposed_debit=Decimal("5"),
                       proposed_fees=Decimal(0), run_limits=RUN,
                       available_collateral=Decimal("4"))
    assert "AVAILABLE_COLLATERAL_INSUFFICIENT" in poor.reasons
    missing = check_entry(ledger, TRIAL, window_id="w1", proposed_debit=Decimal("5"),
                          proposed_fees=Decimal(0), run_limits=RUN, available_collateral=None)
    assert "AVAILABLE_COLLATERAL_INSUFFICIENT" in missing.reasons  # unknown cash blocks
    for name in ("l1", "l2"):
        _filled(ledger, name, name, cost="1")
        ledger.record("HOLD_REQUESTED", name, quantity="10")
        ledger.record("HOLD_COMMITTED", name, quantity="10")
        ledger.record("PAYOUT_PROVEN", name, payout_per_share="0")
        ledger.record("EPISODE_COMPLETED", name, reason="zero_payout")
    assert ledger.consecutive_losses() == 2
    paused = check_entry(ledger, TRIAL, window_id="w9", proposed_debit=Decimal("1"),
                         proposed_fees=Decimal(0), run_limits=RUN,
                         available_collateral=Decimal("100"))
    assert "CONSECUTIVE_LOSS_PAUSE" in paused.reasons
    ledger.record("LOSS_PAUSE_ACKNOWLEDGED", OPERATOR_SCOPE, key="ack1", by="operator")
    assert SettlementLedger(tmp_path / "l.jsonl").consecutive_losses() == 0
    for name in ("u1", "u2"):
        _filled(ledger, name, name, cost="1")
        ledger.record("HOLD_REQUESTED", name, quantity="10")
        ledger.record("HOLD_COMMITTED", name, quantity="10")
        ledger.record("PAYOUT_PROVEN", name, payout_per_share="1")
        ledger.record("REDEMPTION_REQUESTED", name, mode="operator")
    capped = check_entry(ledger, TRIAL, window_id="w9", proposed_debit=Decimal("1"),
                         proposed_fees=Decimal(0), run_limits=RUN,
                         available_collateral=Decimal("100"))
    assert "MAX_UNREDEEMED_COUNT" in capped.reasons


def test_retry_clock_and_policy_survive_reload(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = SettlementLedger(path)
    reserve(
        ledger, "e", "w1",
        lifecycle_policy="hold_to_resolution",
        entry_started_at="2026-10-20T12:00:00+00:00",
    )
    ledger.record(
        "DISPATCH_NO_FILL", "e", posted=True, attempt=1,
        retry_clock="deadline", retry_not_before="2026-10-20T12:00:01+00:00",
        entry_started_at="2026-10-20T12:00:00+00:00", entry_side="UP",
    )
    restored = SettlementLedger(path).episodes["e"]
    assert restored.lifecycle_policy == "hold_to_resolution"
    assert restored.entry_started_at == "2026-10-20T12:00:00+00:00"
    assert restored.retry_clock == "deadline"
    assert restored.retry_not_before == "2026-10-20T12:00:01+00:00"
    assert restored.entry_side == "UP"
    assert restored.posts == 1
    assert restored.consecutive_service_failures is None
    ledger.record(
        "ATTEMPT_REARMED", "e",
        key=SettlementLedger.attempt_key("ATTEMPT_REARMED", "e", 2),
        attempt=2, max_debit="5", max_fees="0",
    )
    ledger.record(
        "DISPATCH_NOT_SENT", "e",
        key=SettlementLedger.attempt_key("DISPATCH_NOT_SENT", "e", 2),
        prepare_failed=True, attempt=2, retry_clock="deadline",
        retry_not_before="2026-10-20T12:00:05+00:00", entry_side="UP",
        consecutive_service_failures=2,
    )
    again = SettlementLedger(path).episodes["e"]
    assert again.consecutive_service_failures == 2
    assert again.prepare_failures == 1


def test_older_no_fill_has_no_invented_backoff(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = SettlementLedger(path)
    reserve(ledger, "e", "w1")
    ledger.record("DISPATCH_NO_FILL", "e", posted=True, attempt=1)
    restored = SettlementLedger(path).episodes["e"]
    assert restored.retry_clock is None
    assert restored.opened_at


def test_run_limits_require_utc_and_positive_values() -> None:
    with pytest.raises(ValueError, match="UTC"):
        RunLimits(daily_loss_limit=Decimal(1), max_open_worst_case_loss=Decimal(1),
                  max_reserved_debit=Decimal(1), max_unredeemed_count=1,
                  max_unredeemed_cost=Decimal(1), max_consecutive_losses=0,
                  accounting_timezone="Europe/Paris")
