"""Ordered multi-window replay: one shared account, delayed payouts, account limits."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from tyrex_pm.research.replay.config import Scenario
from tyrex_pm.research.strategy_replay.sequence import SequenceLimits, run_sequence

NS = 1_000_000_000


def window(i: int):
    return SimpleNamespace(ref=SimpleNamespace(market_id=f"m{i}", slug=f"w{i}",
                                               window_start_ns=i * 300 * NS))


def fake_runner(debit="5", payout="10", proceeds="0", held="10"):
    calls = []

    def run_one(w, *, strategy_config, scenario, **_kw):
        calls.append((w.ref.slug, scenario.initial_cash))
        start = w.ref.window_start_ns
        return {
            "runner_version": "fake",
            "economics": {"entered": True, "filled": True, "debit": debit,
                          "exit_proceeds_net": proceeds, "final_net_shares": held,
                          "payout": payout, "realized_pnl": None},
            "label": {"available_at_ns": start + 300 * NS},
            "attempts": [],
        }

    return run_one, calls


def test_capital_locked_until_redemption_skips_later_windows() -> None:
    run_one, calls = fake_runner()
    result = run_sequence(
        [window(i) for i in range(4)],
        strategy_config=None,
        scenario=Scenario(adapter="strategy_closed_loop_v1"),
        limits=SequenceLimits(max_unresolved_count=9, max_unredeemed_count=9,
                              redemption_delay_s=3600),
        initial_cash=Decimal("12"),
        run_one=run_one,
    )
    # Independent replays would each start with 12; the shared account cannot.
    assert [c[1] for c in calls] == ["12", "7"]
    assert result["skipped_by_reason"] == {"AVAILABLE_COLLATERAL_INSUFFICIENT": 2}


def test_payout_released_before_a_window_funds_it() -> None:
    run_one, calls = fake_runner()
    result = run_sequence(
        [window(0), window(20)],
        strategy_config=None,
        scenario=Scenario(adapter="strategy_closed_loop_v1"),
        limits=SequenceLimits(redemption_delay_s=60),
        initial_cash=Decimal("5"),
        run_one=run_one,
    )
    assert [c[1] for c in calls] == ["5", "10"]  # 5 - 5 + payout 10
    assert result["traded_windows"] == 2


def test_unresolved_and_consecutive_loss_pauses() -> None:
    run_one, _ = fake_runner(payout="0")
    paused = run_sequence(
        [window(i) for i in range(3)],
        strategy_config=None,
        scenario=Scenario(adapter="strategy_closed_loop_v1"),
        limits=SequenceLimits(max_unresolved_count=1),
        initial_cash=Decimal("100"),
        run_one=run_one,
    )
    assert paused["skipped_by_reason"] == {"MAX_UNRESOLVED_COUNT": 2}
    sold, _ = fake_runner(debit="5", proceeds="2", held="0", payout="0")
    losing = run_sequence(
        [window(i) for i in range(4)],
        strategy_config=None,
        scenario=Scenario(adapter="strategy_closed_loop_v1"),
        limits=SequenceLimits(max_consecutive_losses=2),
        initial_cash=Decimal("100"),
        run_one=sold,
    )
    assert losing["skipped_by_reason"] == {"CONSECUTIVE_LOSS_PAUSE": 2}
