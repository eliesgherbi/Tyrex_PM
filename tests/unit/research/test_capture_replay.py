"""Captured live run -> warehouse -> decision reconstruction: adapters and policies."""

from __future__ import annotations

import json
from dataclasses import replace
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


def test_applied_rest_sync_anchor_is_distinct_from_an_http_audit():
    from tyrex_pm.data_warehouse.reconstruction.books import Books

    payload = {"event_type": "book", "asset_id": "111", "market": "0xabc",
               "timestamp": "1792413496000", "capture_sync_health": "SYNCING",
               "bids": [{"price": ".4", "size": "5"}],
               "asks": [{"price": ".6", "size": "5"}]}
    envelope = replace(_env(json.dumps(payload), "clob"),
                       stream_id="captured_applied_rest", transport="collector_event")
    rows = [(name, row) for name, row in normalize(envelope) if name == "book_snapshots"]
    assert rows[0][1]["snapshot_origin"] == "captured_rest_syncing"
    books = Books(reconstruction="venue_top_v1")
    books.apply(*rows[0])
    iid = rows[0][1]["instrument_id"]
    assert not books.books[iid].valid
    payload = {"event_type": "price_change", "market": "0xabc", "timestamp": "1792413497000",
               "price_changes": [{"asset_id": "111", "side": "BUY", "price": ".41",
                                  "size": "5", "best_bid": ".41", "best_ask": ".6"}]}
    for name, row in normalize(_env(json.dumps(payload), "clob")):
        if name == "book_updates":
            books.apply(name, row)
    assert books.books[iid].valid


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


def test_missing_window_or_incomplete_capture_cannot_claim_reconstruction_equal():
    from tyrex_pm.research.strategy_replay.capture_replay import _reconstruction_equal

    complete = {"inputs_captured": True}
    good = {"comparison": {"fully_equal": 2, "live_evaluations": 2,
                           "replayed_evaluations": 2}}
    assert _reconstruction_equal([good], complete)
    assert not _reconstruction_equal([good, {"error": "no gamma metadata imported"}], complete)
    assert not _reconstruction_equal([good], {"inputs_captured": False})
    assert not _reconstruction_equal([], complete)


def test_trading_import_has_its_own_identity_and_observation_identity_is_preserved(tmp_path):
    from tyrex_pm.research.strategy_replay.capture_replay import capture_identity

    raw = tmp_path / "recording/raw"
    raw.mkdir(parents=True)
    (raw / "part.manifest.json").write_text(json.dumps({"sha256": "example"}))
    trading = capture_identity(tmp_path)
    (tmp_path / "observation_manifest.json").write_text("{}")
    observation = capture_identity(tmp_path)
    import hashlib

    expected = hashlib.sha256(json.dumps(["capture_import_v1", ["example"]]).encode())
    assert observation == expected.hexdigest()[:16]
    assert trading != observation


def test_unsupported_trading_capture_has_a_saved_failure_report(tmp_path):
    from tyrex_pm.research.strategy_replay.capture_replay import reconstruct

    result = reconstruct(tmp_path / "old_capture", mode="trading", out=tmp_path / "report")
    assert not result["decisions_reconstructed_equal"]
    assert result["error"] == "unsupported or incomplete trading evidence"
    assert (tmp_path / "report/reconstruction.json").exists()


def test_capture_cursor_links_host_decision_to_the_imported_prefix(tmp_path):
    from tyrex_pm.recording.config import RecordingConfig
    from tyrex_pm.recording.raw import RawCapture
    from tyrex_pm.recording.recorder import Recorder
    from tyrex_pm.research.strategy_replay.capture_replay import capture_ingest_prefixes

    recorder = Recorder(tmp_path / "recording", RecordingConfig(raw_inputs=True),
                        streams={"raw": "tyrex_raw_input_v1"}, manifest={})
    recorder.start()
    raw = RawCapture(recorder)
    raw.event("bindings", "activated", {"binding": {}})
    raw.event("trading_context", "context", {"evaluation_id": 1})
    raw.event("trading_decision", "decision", {"evaluation_id": 1})
    raw.event("bindings", "activated", {"binding": {}})
    recorder.close()
    root = tmp_path / "warehouse"
    (root / "collections/c").mkdir(parents=True)
    assert capture_ingest_prefixes(tmp_path, {"root": str(root), "collection": "c",
                                             "records_written": 2}) == {1: 1}


def test_recorded_sequence_prevents_a_later_tied_input_from_leaking_into_a_decision():
    from pathlib import Path

    from tyrex_pm.research.replay.config import load_scenario
    from tyrex_pm.research.replay.engine import DECISION, Engine, captured_priority

    class ProbeEngine(Engine):
        def observation(self, dataset, row):
            pass

    class ProbeAgent:
        strategy_datasets = {"test_input"}

        def __init__(self, ordered):
            self.recorded_sequence_order = ordered
            self.seen, self.at_decision = [], None

        def start(self, engine, horizon):
            priority = captured_priority(1, DECISION) if self.recorded_sequence_order else DECISION
            engine.schedule(100, priority, "decision", None)

        def handle(self, kind, payload):
            if kind == "strategy_observation":
                self.seen.append(payload[1]["ingest_sequence"])
            elif kind == "decision":
                self.at_decision = list(self.seen)

    scenario = load_scenario(Path("config/research/scenarios/favorable.yaml"))
    events = [("test_input", {"received_at": 100, "ingest_sequence": i}) for i in (1, 2)]
    for ordered, expected in ((True, [1]), (False, [1, 2])):
        agent = ProbeAgent(ordered)
        ProbeEngine(scenario).run_closed_loop(events, agent)
        assert agent.at_decision == expected
