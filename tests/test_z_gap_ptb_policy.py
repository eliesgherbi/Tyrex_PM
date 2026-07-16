"""PTB source precedence and mismatch tests (D2)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tyrex_pm.ingestion.price_to_beat_tracker import (
    PTB_STATUS_LATE,
    PTB_STATUS_OBSERVED,
    PTB_STATUS_OBSERVED_FROM_LOG,
    PtbDerivation,
)
from tyrex_pm.state.z_gap_ptb_store import ZGapPtbStore, ZGapPtbWindowRecord
from tyrex_pm.strategies.z_gap.ptb_policy import (
    PTB_SOURCE_LIVE,
    PTB_SOURCE_LOG,
    PTB_SOURCE_NONE,
    persist_ptb_selection,
    select_ptb_source,
)


def _log_derivation(*, price: str, lag_ms: float = 100.0) -> PtbDerivation:
    return PtbDerivation(
        price=price,
        source_ts=datetime.fromtimestamp(1780000001.0, tz=timezone.utc),
        boundary_lag_ms=lag_ms,
        status=PTB_STATUS_OBSERVED_FROM_LOG,
    )


def test_live_selected_when_valid() -> None:
    r = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        live_price="100000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=200.0,
        log_derivation=_log_derivation(price="99999.00"),
    )
    assert r.selected_source == PTB_SOURCE_LIVE
    assert r.selected_k == "100000.00"
    assert r.usable is True


def test_log_selected_when_live_unavailable() -> None:
    r = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        log_derivation=_log_derivation(price="100001.00"),
    )
    assert r.selected_source == PTB_SOURCE_LOG
    assert r.selected_k == "100001.00"


def test_sources_agree_within_tolerance() -> None:
    r = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        live_price="100000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=100.0,
        log_derivation=_log_derivation(price="100000.01"),
    )
    assert r.usable is True
    assert r.mismatch is False


def test_mismatch_above_half_bps_blocks() -> None:
    r = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        live_price="100000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=100.0,
        log_derivation=_log_derivation(price="100010.00"),
    )
    assert r.mismatch is True
    assert r.usable is False
    assert r.selected_source == PTB_SOURCE_NONE


def test_late_cannot_replace_usable_source(tmp_path) -> None:
    existing = ZGapPtbWindowRecord(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        selected_source=PTB_SOURCE_LOG,
        selected_k="100000.00",
        usable=True,
        locked=True,
    )
    store = ZGapPtbStore(path=tmp_path / "ptb.json")
    store.save(existing)
    r = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        live_price="100500.00",
        live_status=PTB_STATUS_LATE,
        live_lag_ms=9000.0,
        existing=existing,
    )
    assert r.selected_k == "100000.00"
    assert r.locked is True


def test_locked_k_cannot_change(tmp_path) -> None:
    store = ZGapPtbStore(path=tmp_path / "ptb.json")
    first = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        log_derivation=_log_derivation(price="100000.00"),
    )
    persist_ptb_selection(first, store=store)
    second = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        live_price="100001.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=50.0,
        existing=store.load("m1"),
    )
    assert second.selected_k == "100000.00"
    with pytest.raises(ValueError, match="PTB lock violation"):
        persist_ptb_selection(
            select_ptb_source(
                market_id="m1",
                event_start_ts=1780000000.0,
                event_end_ts=1780000300.0,
                live_price="100002.00",
                live_status=PTB_STATUS_OBSERVED,
                live_lag_ms=50.0,
            ),
            store=store,
        )


def test_malformed_sidecar_does_not_affect_policy_selection() -> None:
    r = select_ptb_source(
        market_id="m1",
        event_start_ts=1780000000.0,
        event_end_ts=1780000300.0,
        live_price="100000.00",
        live_status=PTB_STATUS_OBSERVED,
        live_lag_ms=100.0,
        log_derivation=None,
    )
    assert r.selected_k == "100000.00"
