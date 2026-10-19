"""Host decision recording: decision-time records, explain selection, status (plan P4)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tyrex_pm.recording import RecordingConfig, RecordingLevel
from tyrex_pm.recording.config import ExplainTrigger
from tyrex_pm.recording.host import open_decision_recording
from tyrex_pm.recording.records import iter_records
from tyrex_pm.strategies.settlement_value.record_schema import DECISION_RECORD_SCHEMA

AT = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)


class _Graph:
    def __init__(self) -> None:
        self.variance = 1.0

    def explain(self) -> dict:
        return {"ewma": {"variance": self.variance, "sample_count": 3}}


def _ready() -> SimpleNamespace:
    fresh = SimpleNamespace(is_fresh=True, age_ms=40, threshold_ms=5000,
                            timestamp_basis=SimpleNamespace(value="EVENT_TIME"),
                            reason_code=SimpleNamespace(value="FRESH"))
    leg = SimpleNamespace(source_ts=AT, receive_ts=AT, apply_ts=AT, data_age_ms=12,
                          sync_health=SimpleNamespace(value="HEALTHY"), book_version=7)
    snapshot = SimpleNamespace(observed_at=AT, yes_freshness=fresh, no_freshness=fresh,
                               reference_freshness=fresh,
                               book_view=SimpleNamespace(up=leg, down=leg, connection_epoch=2))
    return SimpleNamespace(snapshot=snapshot, binance_source_ts=AT,
                           dyn=SimpleNamespace(chainlink_source_ts=AT))


def _open(tmp_path: Path, **overrides):
    config = RecordingConfig(**{"level": RecordingLevel.EXPLAIN,
                                "explain_trigger": ExplainTrigger.ENTRY_BAND,
                                "explain_context_evaluations": 2, **overrides})
    return open_decision_recording(config, output_directory=tmp_path, mode="live", run_id="r",
                                   schema=DECISION_RECORD_SCHEMA, entry_band=(30.0, 150.0),
                                   manifest={"strategy_kind": "settlement_value"})


def test_essential_level_records_nothing(tmp_path: Path) -> None:
    assert _open(tmp_path, level=RecordingLevel.ESSENTIAL) is None
    assert not (tmp_path / "recording").exists()


def test_decision_and_explain_records_keep_decision_time_values(tmp_path: Path) -> None:
    recording = _open(tmp_path)
    graph = _Graph()
    row = {"at_ns": 1, "tau_s": 200.0, "action": "WAIT", "reason": "EDGE_BELOW_COST",
           "p_up_point": 0.6, "brand_new_diagnostic": 1}
    recording.on_evaluated(_ready(), row, window_id="w1", graph=graph)
    graph.variance = 9.0  # the indicator keeps updating after the first decision
    recording.on_evaluated(_ready(), {**row, "at_ns": 2, "tau_s": 120.0}, window_id="w1",
                           graph=graph)
    graph.variance = 20.0
    recording.on_skipped(_ready(), {"at_ns": 3, "tau_s": 100.0, "action": "HOST_SKIPPED",
                                    "reason": "reference_missing"}, window_id="w1")
    status = recording.close()
    decisions = list(iter_records(tmp_path / "recording", "decisions"))
    assert [d["at_ns"] for d in decisions] == [1, 2, 3]
    first = decisions[0]
    assert first["schema"] == "settlement_value_decision_v2" and first["p_up_point"] == 0.6
    assert "brand_new_diagnostic" not in first
    assert first["inputs"]["up_leg"]["book_version"] == 7
    assert first["inputs"]["reference"]["age_ms"] == 40
    explain = list(iter_records(tmp_path / "recording", "explain"))
    # 200 s is outside the band (context only, flushed by nothing); 120 s is in band.
    # The host skip carries no graph, so its explanation has no indicators.
    assert [e["at_ns"] for e in explain] == [2, 3]
    assert explain[0]["indicators"]["ewma"]["variance"] == 9.0  # value at decision time
    assert explain[1]["indicators"] == {}
    assert status["undeclared_fields"] == ["brand_new_diagnostic"]
    assert status["capture_cost_us"]["samples"] == 3 and status["capture_errors"] == 0
    assert status["streams"]["decisions"]["written"] == 3


def test_a_faulty_input_is_counted_not_raised(tmp_path: Path) -> None:
    recording = _open(tmp_path)

    class _Broken:
        def explain(self):
            raise RuntimeError("explain failed")

    recording.on_evaluated(_ready(), {"at_ns": 1, "tau_s": 100.0, "action": "WAIT",
                                      "reason": "x"}, window_id="w", graph=_Broken())
    status = recording.close()
    assert status["capture_errors"] == 1 and "explain failed" in status["last_capture_error"]
