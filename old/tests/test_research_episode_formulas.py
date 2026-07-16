"""Golden tests for research episode proxy formulas (M2B.4)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from research.lib.episodes import (
    compute_loser_stop_proceeds,
    compute_recovery_target,
    survivor_path_after_stop,
    synthetic_pair_entry,
)


def test_synthetic_episode_arithmetic_pinned() -> None:
    pair = synthetic_pair_entry(yes_price=Decimal("0.48"), no_price=Decimal("0.51"), size=Decimal("5"))
    assert pair.total == Decimal("4.95")

    stop_pct = Decimal("0.09")
    loser_proceeds = compute_loser_stop_proceeds(loser_entry=pair.no_entry, stop_pct=stop_pct)
    assert loser_proceeds == pair.no_entry * Decimal("0.91")

    recovery = compute_recovery_target(entry_cost=pair.total, recovery_buffer=Decimal("0"))
    assert recovery == Decimal("4.95")

    # Survivor YES path — bids never reach recovery (~0.99 per share)
    bba = pd.DataFrame(
        {
            "token_id": ["yes"] * 3,
            "best_bid": [0.45, 0.46, 0.44],
            "source_ts": [
                "2026-07-05T12:00:00+00:00",
                "2026-07-05T12:01:00+00:00",
                "2026-07-05T12:02:00+00:00",
            ],
            "recv_ts": [
                "2026-07-05T12:00:00+00:00",
                "2026-07-05T12:01:00+00:00",
                "2026-07-05T12:02:00+00:00",
            ],
        }
    )
    end = datetime(2026, 7, 5, 12, 5, tzinfo=timezone.utc)
    ep = survivor_path_after_stop(
        market_id="btc_5m_test",
        survivor_token_id="yes",
        loser_token_id="no",
        yes_entry=pair.yes_entry,
        no_entry=pair.no_entry,
        survivor_is_yes=True,
        stop_pct=stop_pct,
        recovery_buffer=Decimal("0"),
        bba=bba,
        event_end_ts=end,
    )
    assert ep.entry_cost == Decimal("4.95")
    assert ep.loser_stop_proceeds == loser_proceeds
    assert ep.recovery_target == recovery
    assert ep.recovery_touched is False
    assert ep.never_armed_and_lost is True


def test_survivor_touch_when_bid_reaches_recovery() -> None:
    pair = synthetic_pair_entry(yes_price=Decimal("0.48"), no_price=Decimal("0.51"), size=Decimal("5"))
    recovery_per_share = compute_recovery_target(entry_cost=pair.total) / Decimal("5")
    bba = pd.DataFrame(
        {
            "token_id": ["yes", "yes"],
            "best_bid": [0.50, float(recovery_per_share)],
            "source_ts": ["2026-07-05T12:00:00+00:00", "2026-07-05T12:01:00+00:00"],
            "recv_ts": ["2026-07-05T12:00:00+00:00", "2026-07-05T12:01:00+00:00"],
        }
    )
    ep = survivor_path_after_stop(
        market_id="btc_5m_touch",
        survivor_token_id="yes",
        loser_token_id="no",
        yes_entry=pair.yes_entry,
        no_entry=pair.no_entry,
        survivor_is_yes=True,
        stop_pct=Decimal("0.09"),
        recovery_buffer=Decimal("0"),
        bba=bba,
        event_end_ts=datetime(2026, 7, 5, 12, 5, tzinfo=timezone.utc),
    )
    assert ep.recovery_touched is True
    assert ep.never_armed_and_lost is False
