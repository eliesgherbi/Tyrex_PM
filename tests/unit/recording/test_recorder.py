"""Recorder correctness, bounded resources and failure handling (plan P4)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tyrex_pm.recording import ExplainTrigger, Recorder, RecordingConfig, RecordingLevel
from tyrex_pm.recording.config import recording_config_from_mapping
from tyrex_pm.recording.recorder import estimate_size
from tyrex_pm.recording.records import ExplainSelector, decision_record, iter_records


def make(tmp_path: Path, **overrides) -> Recorder:
    config = RecordingConfig(**{"shutdown_timeout_s": 5.0, **overrides})
    recorder = Recorder(tmp_path / "rec", config, streams={"decisions": "test_v1"},
                        manifest={"run_id": "r1", "mode": "test"})
    recorder.start()
    return recorder


def test_records_round_trip_with_manifests(tmp_path: Path) -> None:
    recorder = make(tmp_path)
    for i in range(500):
        assert recorder.capture("decisions", {"at_ns": i, "p": i / 1000})
    status = recorder.close()
    stream = status["streams"]["decisions"]
    assert stream["written"] == 500 and stream["dropped"] == 0 and stream["state"] == "closed"
    rows = list(iter_records(tmp_path / "rec", "decisions"))
    assert [r["at_ns"] for r in rows] == list(range(500))
    manifest = json.loads((tmp_path / "rec" / "manifest.json").read_text())
    assert manifest["run_id"] == "r1" and manifest["streams"] == {"decisions": "test_v1"}
    seg = json.loads(next((tmp_path / "rec" / "decisions").glob("*.manifest.json")).read_text())
    assert seg["records"] == 500 and len(seg["sha256"]) == 64
    assert json.loads((tmp_path / "rec" / "status.json").read_text())["dropped"] == 0


def test_capture_keeps_decision_time_values(tmp_path: Path) -> None:
    recorder = make(tmp_path)
    state = {"variance": 1.0}
    recorder.capture("decisions", dict(state))  # the host passes a fresh copy
    state["variance"] = 2.0  # the indicator keeps updating
    recorder.close()
    (row,) = iter_records(tmp_path / "rec", "decisions")
    assert row["variance"] == 1.0


@pytest.mark.parametrize("policy,kept", [("drop_newest", [0, 1]), ("drop_oldest", [2, 3])])
def test_overflow_is_bounded_counted_and_leaves_a_gap(tmp_path: Path, policy, kept) -> None:
    recorder = Recorder(tmp_path / "rec", RecordingConfig(queue_max_records=2, overflow=policy),
                        streams={"decisions": "t"}, manifest={})
    # Not started: nothing drains, so the queue bound is exercised deterministically.
    for i in range(4):
        recorder.capture("decisions", {"at_ns": i}, capture_ns=i * 10**9)
    snap = recorder.status()["streams"]["decisions"]
    assert snap["queue_records"] == 2 and snap["dropped_overflow"] == 2
    assert snap["gaps"] and snap["gaps"][0]["cause"] == "overflow"
    recorder.start()
    status = recorder.close()
    assert status["streams"]["decisions"]["written"] == 2
    assert [r["at_ns"] for r in iter_records(tmp_path / "rec", "decisions")] == kept


def test_byte_bound_includes_variable_sized_explanations(tmp_path: Path) -> None:
    big = {"indicators": {"x": "y" * 10_000}}
    assert estimate_size(big) > 10_000
    recorder = Recorder(tmp_path / "rec", RecordingConfig(queue_max_bytes=25_000),
                        streams={"explain": "t"}, manifest={})
    accepted = [recorder.capture("explain", dict(big)) for _ in range(5)]
    assert accepted.count(True) == 2
    assert recorder.status()["streams"]["explain"]["queue_bytes"] <= 25_000


def test_unserializable_record_is_counted_not_fatal(tmp_path: Path) -> None:
    recorder = make(tmp_path)
    loop = {}
    loop["self"] = loop  # circular: json fails
    recorder.capture("decisions", loop)
    recorder.capture("decisions", {"ok": 1})
    status = recorder.close()["streams"]["decisions"]
    assert status["serialize_failed"] == 1 and status["written"] == 1
    assert status["state"] == "closed"


def test_write_failure_marks_the_stream_failed_and_capture_keeps_returning(
    tmp_path: Path, monkeypatch
) -> None:
    from tyrex_pm.recording import recorder as module

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(module._Segment, "write", broken)
    recorder = make(tmp_path)
    recorder.capture("decisions", {"a": 1})
    deadline = time.time() + 5
    while recorder.status()["streams"]["decisions"]["state"] != "failed":
        assert time.time() < deadline
        time.sleep(0.01)
    started = time.perf_counter()
    assert recorder.capture("decisions", {"a": 2}) is False  # never blocks or raises
    assert time.perf_counter() - started < 0.05
    status = recorder.close()
    stream = status["streams"]["decisions"]
    assert status["failed"] and "disk full" in stream["last_error"]
    assert stream["dropped_after_failure"] >= 1


def test_writer_death_is_visible_through_status(tmp_path: Path, monkeypatch) -> None:
    recorder = Recorder(tmp_path / "rec", RecordingConfig(), streams={"decisions": "t"},
                        manifest={})

    def die():
        raise RuntimeError("writer crashed")

    monkeypatch.setattr(recorder, "_run", die)
    recorder.start()
    recorder._thread.join(2)
    status = recorder.status()
    assert status["writer_alive"] is False and status["failed"]


def test_close_is_bounded_and_counts_the_rest(tmp_path: Path, monkeypatch) -> None:
    from tyrex_pm.recording import recorder as module

    release = threading.Event()
    real_write = module._Stream.write

    def slow(self, batch):
        release.wait(5)
        real_write(self, batch)

    monkeypatch.setattr(module._Stream, "write", slow)
    recorder = make(tmp_path, shutdown_timeout_s=0.2)
    recorder.capture("decisions", {"first": 1})
    time.sleep(0.6)  # the writer drains on its timer, takes the record and blocks
    for i in range(10):
        recorder.capture("decisions", {"i": i})
    started = time.perf_counter()
    status = recorder.close()
    assert time.perf_counter() - started < 1.0
    stream = status["streams"]["decisions"]
    assert stream["dropped_at_shutdown"] == 10  # still queued
    assert stream["unconfirmed_at_shutdown"] == 1  # held by the stuck writer
    assert stream["dropped"] == 11 and stream["written"] == 0
    assert status["writer_finished"] is False
    release.set()


def test_records_in_a_segment_that_cannot_be_closed_are_not_counted_written(
    tmp_path: Path, monkeypatch
) -> None:
    from tyrex_pm.recording import recorder as module

    def broken_close(self):
        raise OSError("disk full at flush")

    monkeypatch.setattr(module._Segment, "close", broken_close)
    recorder = make(tmp_path)
    for i in range(5):
        recorder.capture("decisions", {"i": i})
    status = recorder.close()["streams"]["decisions"]
    assert status["written"] == 0 and status["lost_in_failed_segment"] == 5
    assert status["state"] == "failed" and status["dropped"] == 5


def test_explain_selector_entry_band_and_bounded_context() -> None:
    selector = ExplainSelector(ExplainTrigger.ENTRY_BAND, context_evaluations=2,
                               entry_band=(30.0, 150.0))
    assert selector.offer({"at_ns": 1}, action="WAIT", reason="EDGE_BELOW_COST", tau_s=200) == []
    assert selector.offer({"at_ns": 2}, action="WAIT", reason="EDGE_BELOW_COST", tau_s=190) == []
    assert selector.offer({"at_ns": 3}, action="WAIT", reason="EDGE_BELOW_COST", tau_s=180) == []
    fault = selector.offer({"at_ns": 4}, action="WAIT", reason="MODEL_NOT_READY", tau_s=170)
    assert [r["at_ns"] for r in fault] == [2, 3, 4]  # two preceding evaluations
    assert fault[0]["context_for_at_ns"] == 4
    inside = selector.offer({"at_ns": 5}, action="WAIT", reason="EDGE_BELOW_COST", tau_s=120)
    assert [r["at_ns"] for r in inside] == [5]
    everything = ExplainSelector(ExplainTrigger.ALL, context_evaluations=0, entry_band=None)
    assert everything.offer({"at_ns": 9}, action="WAIT", reason="x", tau_s=999) == [{"at_ns": 9}]


def test_declared_schema_projection_reports_undeclared_fields() -> None:
    from tyrex_pm.recording.records import RecordField, RecordSchema

    schema = RecordSchema("s", 1, (RecordField("p", "number"),))
    undeclared: set[str] = set()
    row = decision_record({"p": 0.5, "surprise": 1, "action": "WAIT", "at_ns": 1},
                          mode="live", run_id="r", window_id="w", schema=schema,
                          inputs={"decision_at": "t"}, undeclared=undeclared)
    assert row["p"] == 0.5 and "surprise" not in row and undeclared == {"surprise"}
    assert row["schema"] == "s_v1" and row["inputs"] == {"decision_at": "t"}


def test_config_parsing_and_validation() -> None:
    cfg = recording_config_from_mapping({"level": "explain", "raw_inputs": "on",
                                         "queue_max_mib": 1})
    assert cfg.level is RecordingLevel.EXPLAIN and cfg.raw_inputs
    assert cfg.queue_max_bytes == 1024 * 1024
    assert RecordingConfig().level is RecordingLevel.DECISIONS
    with pytest.raises(ValueError):
        recording_config_from_mapping({"level": "everything"})
    with pytest.raises(ValueError):
        recording_config_from_mapping({"bogus": 1})
