"""N2 correction: receive timing contract + Up/Down compatibility."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tyrex_pm.adapters.binance.normalize import normalize_trade_message
from tyrex_pm.adapters.polymarket.discovery import bind_btc_5m_gamma_event
from tyrex_pm.adapters.polymarket.rtds_normalize import normalize_chainlink_tick
from tyrex_pm.core.clock import FakeClock
from tyrex_pm.core.ingress import build_ingress_timing
from tyrex_pm.core.time_authority import (
    ClockSyncSnapshot,
    SnapshotTimeAuthority,
    TimeSyncStatus,
)
from tyrex_pm.domain.polymarket.discovery_binding import DiscoverySessionRole
from tyrex_pm.domain.polymarket.outcome_map import NormalizedLeg, map_up_down_outcomes

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / "fixtures" / "n2"


def test_build_ingress_timing_applies_known_offset() -> None:
    raw = datetime(2026, 7, 21, 8, 0, 0, tzinfo=timezone.utc)
    clock = FakeClock(_wall=raw)
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=1000)
    snap = ClockSyncSnapshot(
        measured_at_wall_utc=raw,
        measured_at_monotonic_ns=1,
        estimated_offset_ms=250.0,
        uncertainty_ms=10,
        sync_status=TimeSyncStatus.READY,
        primary_source="fake",
        snapshot_id="snap-1",
    )
    auth.apply_snapshot(snap)
    view = auth.view()
    meta = build_ingress_timing(
        receive_wall_raw_utc=raw,
        receive_monotonic_ns=99,
        ingress_sequence=1,
        connection_generation=1,
        time_view=view,
        clock_snapshot_id=view.clock_snapshot_id,
    )
    assert meta.receive_wall_raw_utc == raw
    assert meta.receive_wall_corrected_utc == raw + timedelta(milliseconds=250)
    assert meta.clock_offset_ms == 250.0
    assert meta.clock_status == "READY"
    assert meta.clock_snapshot_id == "snap-1"


def test_normalize_trade_keeps_ts_received_raw_and_sets_corrected() -> None:
    raw = datetime(2026, 7, 21, 8, 0, 1, tzinfo=timezone.utc)
    clock = FakeClock(_wall=raw)
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=5000)
    auth.apply_snapshot(
        ClockSyncSnapshot(
            measured_at_wall_utc=raw,
            measured_at_monotonic_ns=1,
            estimated_offset_ms=-400.0,
            uncertainty_ms=50,
            sync_status=TimeSyncStatus.READY,
            primary_source="fake",
        )
    )
    view = auth.view()
    payload = {"s": "BTCUSDT", "p": "65000.0", "T": 1784616001000, "t": 42}
    evt = normalize_trade_message(
        payload,
        ts_received=raw,
        receive_monotonic_ns=7,
        ingress_sequence=3,
        connection_generation=2,
        time_view=view,
    )
    assert evt.ts_received == raw
    assert evt.ingress is not None
    assert evt.ingress.receive_wall_raw_utc == raw
    assert evt.ingress.receive_wall_corrected_utc == raw + timedelta(milliseconds=-400)
    assert evt.ingress.clock_offset_ms == -400.0
    # Corrected latency vs source can be negative when host clock is ahead of exchange
    # after applying a negative offset, or when exchange T is ahead of host — both reported.
    raw_lag_ms = (evt.ts_received - evt.ts_event).total_seconds() * 1000.0
    corr_lag_ms = (
        evt.ingress.receive_wall_corrected_utc - evt.ts_event
    ).total_seconds() * 1000.0
    assert corr_lag_ms == pytest.approx(raw_lag_ms - 400.0)


def test_degraded_status_propagates_into_ingress() -> None:
    raw = datetime(2026, 7, 21, 8, 0, 0, tzinfo=timezone.utc)
    clock = FakeClock(_wall=raw)
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=100)
    auth.apply_snapshot(
        ClockSyncSnapshot(
            measured_at_wall_utc=raw,
            measured_at_monotonic_ns=1,
            estimated_offset_ms=10.0,
            uncertainty_ms=5,
            sync_status=TimeSyncStatus.DEGRADED,
            primary_source="fake",
        )
    )
    meta = build_ingress_timing(
        receive_wall_raw_utc=raw,
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        time_view=auth.view(),
    )
    assert meta.clock_status == "DEGRADED"


def test_unsynchronized_without_time_view() -> None:
    raw = datetime(2026, 7, 21, 8, 0, 0, tzinfo=timezone.utc)
    meta = build_ingress_timing(
        receive_wall_raw_utc=raw,
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        time_view=None,
    )
    assert meta.clock_status == "UNSYNCHRONIZED"
    assert meta.receive_wall_corrected_utc == raw
    assert meta.clock_offset_ms is None


def test_adapter_cannot_pass_corrected_as_ts_received() -> None:
    raw = datetime(2026, 7, 21, 8, 0, 0, tzinfo=timezone.utc)
    corrected = raw + timedelta(milliseconds=100)
    meta = build_ingress_timing(
        receive_wall_raw_utc=raw,
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        time_view=None,
    )
    # Force mismatch: corrected stuffed into raw field of a hand-built meta is
    # rejected when normalize checks raw == ts_received.
    from tyrex_pm.core.ingress import IngressMeta

    bad = IngressMeta(
        receive_monotonic_ns=1,
        ingress_sequence=1,
        connection_generation=1,
        receive_wall_raw_utc=corrected,
        receive_wall_corrected_utc=corrected,
        clock_status="READY",
        role=meta.role,
    )
    with pytest.raises(ValueError, match="receive_wall_raw_utc must equal"):
        normalize_trade_message(
            {"s": "BTCUSDT", "p": "1", "T": 1784616000000, "t": 1},
            ts_received=raw,
            ingress=bad,
        )


def test_chainlink_normalize_includes_timing_fields() -> None:
    msg = json.loads((FIXTURES / "rtds_chainlink_tick.json").read_text(encoding="utf-8"))
    raw = datetime(2026, 7, 20, 21, 15, 5, tzinfo=timezone.utc)
    clock = FakeClock(_wall=raw)
    auth = SnapshotTimeAuthority(clock=clock, max_uncertainty_ms=5000)
    auth.apply_snapshot(
        ClockSyncSnapshot(
            measured_at_wall_utc=raw,
            measured_at_monotonic_ns=1,
            estimated_offset_ms=100.0,
            uncertainty_ms=20,
            sync_status=TimeSyncStatus.READY,
            primary_source="fake",
        )
    )
    evt = normalize_chainlink_tick(
        msg,
        ts_received=raw,
        receive_monotonic_ns=5,
        ingress_sequence=1,
        connection_generation=1,
        clock_uncertainty_ms=None,
        time_view=auth.view(),
    )
    assert evt is not None
    assert evt.ts_received == raw
    assert evt.ingress is not None
    assert evt.ingress.receive_wall_corrected_utc == raw + timedelta(milliseconds=100)


def test_up_down_public_identity_not_literal_yes_no() -> None:
    event = json.loads((FIXTURES / "gamma_btc_5m_event.json").read_text(encoding="utf-8"))
    # Reverse provider array order
    event["markets"][0]["outcomes"] = '["Down", "Up"]'
    event["markets"][0]["clobTokenIds"] = (
        '["DOWNTOK", "UPTOK"]'
    )
    binding = bind_btc_5m_gamma_event(
        event,
        expected_slug="btc-updown-5m-1784582100",
        session_role=DiscoverySessionRole.ACTIVE,
        require_chainlink_source=True,
    )
    assert binding.outcome_semantics == "UP_DOWN"
    up, down = binding.require_up_down_tokens()
    assert up.value == "UPTOK"
    assert down.value == "DOWNTOK"
    assert binding.up_token_id == "UPTOK"
    assert binding.down_token_id == "DOWNTOK"
    # Compatibility slots: yes←Up, no←Down
    assert binding.market.yes.token_id.value == "UPTOK"
    assert binding.market.no.token_id.value == "DOWNTOK"
    assert "Up/Down" in binding.compatibility_yes_no_note
    legs = {b.leg: b.venue_label for b in binding.book_legs}
    assert legs[NormalizedLeg.UP] == "Up"
    assert legs[NormalizedLeg.DOWN] == "Down"


def test_label_map_never_positional() -> None:
    om = map_up_down_outcomes(["Down", "Up"], ["d", "u"])
    assert om.mapping_method == "label_index_only"
    assert om.token_for(NormalizedLeg.UP).value == "u"
