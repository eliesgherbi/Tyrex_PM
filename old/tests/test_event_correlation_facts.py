"""Event correlation fact field tests (M2B.0-C)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from tyrex_pm.core.ids import RunId
from tyrex_pm.core.time import monotonic_s, utc_now
from tyrex_pm.market_data.quality import DecisionContext
from tyrex_pm.reporting.facts import make_fact
from tyrex_pm.reporting.sinks.jsonl import JsonlSink
from tyrex_pm.runtime.config import ObservabilityConfig, parse_app_config
from tyrex_pm.strategies.paired_binary.facts import (
    EventCorrelationContext,
    build_event_correlation_fields,
    emit_latency_chain,
)
from tyrex_pm.strategies.paired_binary.latency import LatencyChain, LatencyTracker
from tyrex_pm.strategies.paired_binary.observability import emit_material_decision
from tyrex_pm.strategies.paired_binary.state import PairedBinaryPhase, PairedBinaryRuntimeState


def _minimal_app(*, emit_event_correlation: bool = False):
    return parse_app_config(
        risk={
            "notional": {"min_usd": "0.01", "max_usd": "1000", "max_policy": "cap"},
            "deployment": {"token_cap_usd": "5000", "portfolio_cap_usd": "50000"},
            "venue_min_size": {"enabled": False},
            "capital": {"enabled": False},
        },
        strategy={
            "kind": "paired_binary",
            "enabled": True,
            "paired_binary": {
                "owner_id": "paired_binary",
                "market_id": "m1",
                "yes_token_id": "yes",
                "no_token_id": "no",
                "position_size": "5",
                "max_pair_entry_cost": "1.02",
                "use_fixture_book": True,
                "fixture_yes_bid": "0.48",
                "fixture_yes_ask": "0.49",
                "fixture_no_bid": "0.50",
                "fixture_no_ask": "0.51",
            },
        },
        runtime={
            "execution_mode": "shadow",
            "observability": {
                "emit_decision_snapshot": True,
                "emit_event_correlation": emit_event_correlation,
            },
            "market_data": {"enabled": True, "max_book_age_s": 5},
            "execution": {"planner": {"enabled": True}},
        },
    )


class _Coord:
    market_state = None


def test_flag_off_omits_correlation_fields() -> None:
    fields = build_event_correlation_fields(
        EventCorrelationContext(trigger_event_id="evt-1"),
        enabled=False,
    )
    assert fields == {}


def test_flag_on_populates_correlation_fields() -> None:
    recv = datetime(2026, 7, 3, 12, 0, 1, tzinfo=timezone.utc)
    wall = datetime(2026, 7, 3, 12, 0, 2, tzinfo=timezone.utc)
    fields = build_event_correlation_fields(
        EventCorrelationContext(trigger_event_id="abc123", event_recv_ts=recv),
        enabled=True,
        decision_wall_ts=wall,
    )
    assert fields["trigger_event_id"] == "abc123"
    assert fields["event_recv_ts"] == "2026-07-03T12:00:01+00:00"
    assert fields["decision_wall_ts"] == "2026-07-03T12:00:02+00:00"


def test_decision_ts_remains_monotonic_float() -> None:
    tracker = LatencyTracker(decision_id="d-mono")
    before = monotonic_s()
    tracker.mark_decision()
    after = monotonic_s()
    assert tracker.decision_ts is not None
    assert isinstance(tracker.decision_ts, float)
    assert before <= tracker.decision_ts <= after


def test_emit_material_decision_flag_off_no_correlation_keys(tmp_path) -> None:
    app = _minimal_app(emit_event_correlation=False)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE, pair_correlation_id="m1")
        emit_material_decision(
            app=app,
            coord=_Coord(),
            sink=sink,
            run_id=RunId(str(uuid4())),
            cfg=app.paired_binary,
            state=state,
            decision_type="stop_trigger",
            context=DecisionContext.STOP,
            size=Decimal("5"),
            event_correlation=EventCorrelationContext(trigger_event_id="should-not-appear"),
        )
    rows = [json.loads(line) for line in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    latency = next(r for r in rows if r["fact_type"] == "latency_chain")
    assert "trigger_event_id" not in latency["payload"]
    assert "event_recv_ts" not in latency["payload"]
    assert "decision_wall_ts" not in latency["payload"]


def test_emit_material_decision_flag_on_populates_ws_wake_correlation(tmp_path) -> None:
    app = _minimal_app(emit_event_correlation=True)
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        state = PairedBinaryRuntimeState(phase=PairedBinaryPhase.IDLE, pair_correlation_id="m1")
        recv = utc_now()
        emit_material_decision(
            app=app,
            coord=_Coord(),
            sink=sink,
            run_id=RunId(str(uuid4())),
            cfg=app.paired_binary,
            state=state,
            decision_type="stop_trigger",
            context=DecisionContext.STOP,
            size=Decimal("5"),
            event_correlation=EventCorrelationContext(
                trigger_event_id="ws-event-99",
                event_recv_ts=recv,
            ),
        )
    rows = [json.loads(line) for line in (tmp_path / "facts.jsonl").read_text(encoding="utf-8").splitlines()]
    latency = next(r for r in rows if r["fact_type"] == "latency_chain")
    assert latency["payload"]["trigger_event_id"] == "ws-event-99"
    assert latency["payload"]["event_recv_ts"] is not None
    assert latency["payload"]["decision_wall_ts"] is not None
    datetime.fromisoformat(latency["payload"]["decision_wall_ts"])


def test_latency_chain_extra_payload_backward_compatible(tmp_path) -> None:
    with JsonlSink(tmp_path / "facts.jsonl") as sink:
        chain = LatencyChain(
            decision_id="d-old",
            trigger_to_submit_ms=10,
            submit_to_ack_ms=None,
            trigger_to_fill_ms=None,
            ack_to_user_fill_ms=None,
            fill_to_sellable_ms=None,
            market_book_age_ms=None,
            user_ws_age_ms=None,
            wallet_position_age_ms=None,
            source="websocket",
        )
        emit_latency_chain(sink, RunId("run1"), chain=chain)
    row = json.loads((tmp_path / "facts.jsonl").read_text(encoding="utf-8").strip())
    assert row["payload"]["decision_id"] == "d-old"
    assert "trigger_event_id" not in row["payload"]


def test_old_fact_envelope_without_correlation_still_valid() -> None:
    fact = make_fact(
        "latency_chain",
        "run-legacy",
        {
            "decision_id": "legacy-decision",
            "trigger_to_submit_ms": 12,
            "submit_to_ack_ms": None,
            "trigger_to_fill_ms": None,
            "ack_to_user_fill_ms": None,
            "fill_to_sellable_ms": None,
            "market_book_age_ms": 50,
            "user_ws_age_ms": None,
            "wallet_position_age_ms": None,
            "source": "websocket",
        },
    )
    assert fact["schema_version"] == 2
    assert "trigger_event_id" not in fact["payload"]


def test_observability_config_defaults_correlation_off() -> None:
    cfg = ObservabilityConfig()
    assert cfg.emit_event_correlation is False
