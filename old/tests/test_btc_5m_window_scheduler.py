"""Unit tests for BTC 5m trading window scheduler (run_continue)."""

from __future__ import annotations

import pytest

from tyrex_pm.ingestion.btc_5m_window_scheduler import (
    btc_5m_canonical_event_url,
    btc_5m_event_slug,
    current_btc_5m_window_start,
    select_next_btc_5m_trading_window,
    validate_btc_5m_reference_url,
)
from tyrex_pm.venue.polymarket.event_metadata import EventMetadataError

# 300s-aligned base (verified divisible by 300)
_ALIGNED = 1_783_369_800


def test_validate_reference_accepts_fr_polymarket_url() -> None:
    slug = validate_btc_5m_reference_url(
        "https://polymarket.com/fr/event/btc-updown-5m-1783369200"
    )
    assert slug == "btc-updown-5m-1783369200"


def test_validate_reference_rejects_invalid_slug() -> None:
    with pytest.raises(EventMetadataError):
        validate_btc_5m_reference_url("toto")


def test_old_reference_url_does_not_select_past_window() -> None:
    """Scheduler uses wall clock, not reference slug timestamp."""
    old_ref = "https://polymarket.com/event/btc-updown-5m-1000000000"
    validate_btc_5m_reference_url(old_ref)
    now = _ALIGNED + 220  # 03:40 into current window
    plan = select_next_btc_5m_trading_window(now_ts=now, prestart_seconds=30.0)
    assert plan.window_start_ts == _ALIGNED + 300
    assert plan.window_start_ts != 1_000_000_000


def test_select_next_window_at_12_03_40_style_offset() -> None:
    """03:40 into a 5m window → next open + wake 30s before."""
    now = _ALIGNED + 220
    plan = select_next_btc_5m_trading_window(
        now_ts=now,
        prestart_seconds=30.0,
        entry_grace_seconds=15.0,
    )
    assert plan.window_start_ts == _ALIGNED + 300
    assert plan.window_end_ts == _ALIGNED + 600
    assert plan.wake_at_ts == _ALIGNED + 270
    assert plan.event_slug == btc_5m_event_slug(_ALIGNED + 300)
    assert plan.event_url == btc_5m_canonical_event_url(_ALIGNED + 300)


def test_wake_clamped_to_now_when_past_scheduled_wake() -> None:
    now = _ALIGNED + 275  # after wake_at (_ALIGNED+270) but before open
    plan = select_next_btc_5m_trading_window(now_ts=now, prestart_seconds=30.0)
    assert plan.wake_at_ts == now


def test_late_now_targets_following_window_not_current() -> None:
    """20s into a 5m window → trade the next boundary, not the in-progress window."""
    now = _ALIGNED + 320
    plan = select_next_btc_5m_trading_window(
        now_ts=now,
        prestart_seconds=30.0,
        entry_grace_seconds=15.0,
    )
    assert plan.window_start_ts == _ALIGNED + 600
    assert plan.skip_reason is None


def test_current_btc_5m_window_start_aligns_to_300s() -> None:
    assert current_btc_5m_window_start(_ALIGNED + 220) == _ALIGNED
    assert current_btc_5m_window_start(_ALIGNED) == _ALIGNED


def test_generated_slug_and_url() -> None:
    start = _ALIGNED + 300
    assert btc_5m_event_slug(start) == f"btc-updown-5m-{start}"
    assert btc_5m_canonical_event_url(start) == f"https://polymarket.com/event/btc-updown-5m-{start}"
