"""Tests for BTC 5m session window selection."""

from __future__ import annotations

from tyrex_pm.ingestion.btc_5m_window_scheduler import (
    select_btc_5m_session_window,
    select_next_btc_5m_trading_window,
)
from tyrex_pm.ingestion.market_discovery import BTC_5M_WINDOW_S

_ALIGNED = 1_784_145_000


def test_hard_min_18s_skips_to_next_window() -> None:
    now = _ALIGNED + 282  # 18s before next open at _ALIGNED+300
    plan, skipped = select_btc_5m_session_window(
        now_ts=now,
        target_prestart_seconds=90.0,
        hard_min_prestart_seconds=20.0,
    )
    assert skipped
    assert skipped[0].lead_time_s == 18.0
    assert plan.window_start_ts == _ALIGNED + 600


def test_target_30s_skips_even_if_above_hard_min() -> None:
    now = _ALIGNED + 260  # 40s lead to +300, below 90 target
    plan, skipped = select_btc_5m_session_window(
        now_ts=now,
        target_prestart_seconds=90.0,
        hard_min_prestart_seconds=20.0,
    )
    assert skipped
    assert skipped[0].reason == "insufficient_prestart_target"
    assert plan.window_start_ts == _ALIGNED + 600


def test_sufficient_target_selects_window() -> None:
    now = _ALIGNED  # 300s before +300
    plan, skipped = select_btc_5m_session_window(
        now_ts=now,
        target_prestart_seconds=90.0,
        hard_min_prestart_seconds=20.0,
    )
    assert not skipped
    assert plan.window_start_ts == _ALIGNED + BTC_5M_WINDOW_S


def test_legacy_scheduler_still_works() -> None:
    now = _ALIGNED + 220
    plan = select_next_btc_5m_trading_window(now_ts=now, prestart_seconds=30.0)
    assert plan.window_start_ts == _ALIGNED + 300
