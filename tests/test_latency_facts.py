"""LatencyChain builder tests."""

from __future__ import annotations

from tyrex_pm.strategies.paired_binary.latency import LatencyChain, LatencyTracker


def test_missing_ack_fill_null_with_reason() -> None:
    tracker = LatencyTracker(decision_id="d-lat")
    tracker.mark_decision()
    tracker.mark_oms_submit()
    chain = tracker.build_chain()
    assert isinstance(chain, LatencyChain)
    assert chain.submit_to_ack_ms is None
    assert chain.trigger_to_fill_ms is None
    assert chain.missing_fields_reason is not None
    assert "oms_ack" in chain.missing_fields_reason


def test_populated_chain() -> None:
    tracker = LatencyTracker(decision_id="d-full")
    tracker.mark_decision()
    tracker.mark_oms_submit()
    tracker.mark_oms_ack()
    tracker.mark_fill_seen()
    tracker.book_age_ms = 120
    tracker.source = "websocket"
    chain = tracker.build_chain()
    assert chain.trigger_to_submit_ms is not None
    assert chain.submit_to_ack_ms is not None
    assert chain.market_book_age_ms == 120
    assert chain.source == "websocket"


def test_payload_serializes_nulls() -> None:
    tracker = LatencyTracker(decision_id="d-p")
    payload = tracker.build_chain().to_payload()
    assert payload["decision_id"] == "d-p"
    assert "trigger_to_submit_ms" in payload
