"""Captured live run -> warehouse -> decision reconstruction: adapters and policies."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from tyrex_pm.core.clock import FakeClock
from tyrex_pm.data_warehouse.contracts import RawEnvelope
from tyrex_pm.data_warehouse.normalize.registry import RTDS_TWAP_FEED_ID, normalize
from tyrex_pm.research.strategy_replay.capture_replay import (
    clock_sample_from_snapshot,
    compare_decisions,
    gamma_markets_from_sdk,
    sdk_to_venue_message,
)
from tyrex_pm.research.strategy_replay.clock_sync import RecordedClockAuthority

RECEIVED = 1_792_413_497_302_000_000


def _env(body: str, source: str, **extra) -> RawEnvelope:
    return RawEnvelope.create(body=body, collection_id="c", session_id="s", sequence=3,
                              source=source, stream="x", connection="1", received_at=RECEIVED,
                              **extra)


def test_rtds_live_tick_and_history_become_reference_observations() -> None:
    tick = {"topic": "crypto_prices_twap_sixty", "type": "update", "timestamp": 1792413497302,
            "payload": {"symbol": "btc/usd", "timestamp": 1792413496000, "value": 82549.7,
                        "full_accuracy_value": "82549708918180865376256"}}
    rows = [r for d, r in normalize(_env(json.dumps(tick), "rtds"))
            if d == "reference_observations"]
    assert len(rows) == 1 and rows[0]["feed_id"] == RTDS_TWAP_FEED_ID
    assert str(rows[0]["value"]) == "82549.708918180865376256"  # full accuracy, as live
    assert rows[0]["received_at"] == RECEIVED and rows[0]["delivery_kind"] != "subscription_history"
    history = {"topic": "crypto_prices_twap_sixty", "type": "subscribe", "timestamp": 1,
               "payload": {"symbol": "btc/usd", "data": [
                   {"timestamp": 1792413400000, "value": 1.0},
                   {"timestamp": 1792413401000, "value": 2.0}]}}
    hist = [r for d, r in normalize(_env(json.dumps(history), "rtds"))
            if d == "reference_observations"]
    assert len(hist) == 2 and all(r["received_at"] == RECEIVED for r in hist)
    assert {r["delivery_kind"] for r in hist} == {"subscription_history"}


def test_sdk_market_events_are_rewritten_as_venue_messages_the_clob_normalizer_reads() -> None:
    dump = {"topic": "market", "type": "price_change", "payload": {
        "market": "0xabc", "timestamp": "2026-10-19T12:38:16.326000Z",
        "price_changes": [{"token_id": "111", "price": "0.03", "size": "15", "side": "BUY",
                           "hash": "h", "best_bid": "0.07", "best_ask": "0.08"}]}}
    message = sdk_to_venue_message(dump)
    assert message["event_type"] == "price_change" and message["timestamp"] == "1792413496326"
    rows = [r for d, r in normalize(_env(json.dumps(message), "clob"))
            if d == "book_updates"]
    (change,) = rows[0]["changes"]
    assert change["instrument_id"].endswith(":111") and str(change["reported_best_ask"]) == "0.08"
    book = sdk_to_venue_message({"type": "book", "payload": {
        "market": "0xabc", "token_id": "111", "bids": [{"price": "0.4", "size": "1"}],
        "asks": [{"price": "0.6", "size": "2"}], "timestamp": "2026-10-19T12:38:16Z"}})
    snaps = [r for d, r in normalize(_env(json.dumps(book), "clob")) if d == "book_snapshots"]
    assert snaps and snaps[0]["snapshot_origin"] == "websocket"


def test_sdk_gamma_events_get_the_slug_the_family_matcher_needs() -> None:
    event = {"slug": "btc-updown-5m-1792413300", "endDate": "2026-10-19T12:40:00Z",
             "markets": [{"conditionId": "0x" + "ab" * 32, "outcomes": '["Up","Down"]',
                          "clobTokenIds": '["1","2"]', "endDate": "2026-10-19T12:40:00Z"}]}
    adapted = gamma_markets_from_sdk(event)
    assert adapted["markets"][0]["slug"] == event["slug"]
    out = normalize(_env(json.dumps(adapted), "gamma", transport="http", http_status=200))
    meta = [r for d, r in out if d == "metadata_versions"]
    assert meta and meta[0]["window_start"] == 1792413300 * 10**9


def test_captured_clock_snapshot_is_applied_as_live() -> None:
    measured = datetime(2026, 10, 19, 12, 38, 14, tzinfo=timezone.utc)
    sample = clock_sample_from_snapshot({
        "measured_at_wall_utc": measured.isoformat(), "estimated_offset_ms": 424.76,
        "uncertainty_ms": 166, "sync_status": "READY", "snapshot_id": "snap-1"})
    clock = FakeClock(_wall=measured)
    authority = RecordedClockAuthority(clock=clock, max_uncertainty_ms=250)
    authority.apply({**sample, "received_at": 0})
    view = authority.view()
    assert view.ready and view.uncertainty_ms == 166 and view.clock_snapshot_id == "snap-1"
    assert abs((view.corrected_utc - measured).total_seconds() - 0.42476) < 1e-6
    clock.advance_to_epoch_ns(int(measured.timestamp() * 1e9) + 61 * 10**9)
    aged = authority.view()  # older than live's 60 s snapshot validity: degraded, as live
    assert not aged.ready and aged.sync_status.value == "DEGRADED"


def test_decision_comparison_counts_fields_and_tolerates_tiny_numeric_noise() -> None:
    live = [{"action": "WAIT", "reason": "X", "p_up_point": 0.5, "tau_s": 100.0},
            {"action": "WAIT", "reason": "X", "p_up_point": 0.5}]
    replay = [{"action": "WAIT", "reason": "X", "p_up_point": 0.5 + 1e-12, "tau_s": 100.3},
              {"action": "ENTER", "reason": "Y", "p_up_point": 0.6}]
    result = compare_decisions(live, replay)
    assert result["decision_equal"] == 1 and result["fully_equal"] == 1
    assert result["field_mismatch_counts"] == {"action": 1, "reason": 1, "p_up_point": 1}
