"""Raw input capture: fidelity labels, adapter tee, completeness, fault policy (plan P5)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from tyrex_pm.recording import RecordingConfig, RecordingLevel
from tyrex_pm.recording.host import open_decision_recording
from tyrex_pm.recording.raw import RawCapture
from tyrex_pm.recording.records import iter_records


class _Sink:
    def __init__(self, fail: bool = False) -> None:
        self.records: list[dict] = []
        self.fail = fail

    def capture(self, stream, record, capture_ns=None):  # noqa: ANN001
        if self.fail:
            raise OSError("boom")
        self.records.append(record)
        return True


@dataclass
class _Model:
    price: Decimal
    side: str


def test_fidelity_is_declared_per_input() -> None:
    sink = _Sink()
    raw = RawCapture(sink)
    raw.message("reference_twap", '{"topic":"crypto_prices_twap_sixty"}', connection=3)
    raw.message("binance_trades", b"\xff\xfe", connection=1)
    raw.message("clob_market", _Model(Decimal("0.42"), "BUY"), connection="b:1")
    raw.event("clock", "clock_sample", {"offset_ms": 1.5})
    text, binary, model, event = sink.records
    assert text["body_kind"] == "byte_exact" and text["body_text"].startswith('{"topic"')
    assert text["source"] == "rtds" and text["connection_id"] == "3"
    assert binary["body_base64"] and binary["body_kind"] == "byte_exact"
    assert model["body_kind"] == "sdk_model" and model["body_json"] == {"price": "0.42",
                                                                       "side": "BUY"}
    assert event["transport"] == "collector_event"
    assert event["body_json"] == {"type": "clock_sample", "offset_ms": 1.5}
    assert [r["seq"] for r in sink.records] == [1, 2, 3, 4]


def test_completeness_is_honest_about_replay_gaps() -> None:
    raw = RawCapture(_Sink())
    raw.message("reference_twap", "x")
    report = raw.completeness(expected=("reference_twap", "clob_market"))
    assert report["classes"]["reference_twap"]["records"] == 1
    assert report["missing_classes"] == ["clob_market"]
    assert report["sdk_model_classes"] == ["clob_market"]
    assert report["inputs_captured"] is False and report["decision_complete"] is False
    assert any("rtds" in reason for reason in report["decision_complete_reasons"])
    raw.not_in_contract("clob_market")
    report = raw.completeness(expected=("reference_twap", "clob_market"))
    assert report["missing_classes"] == [] and report["inputs_captured"] is True
    assert report["not_in_input_contract"] == ["clob_market"]


def test_a_capture_error_stops_capture_visibly_without_raising() -> None:
    faults: list[str] = []
    raw = RawCapture(_Sink(fail=True), on_fault=faults.append)
    raw.message("binance_trades", "a")
    raw.message("binance_trades", "b")  # ignored after the fault
    assert raw.fault and faults == [raw.fault] and raw.capture_errors == 1


def test_rtds_adapter_tees_the_frame_before_parsing() -> None:
    from tyrex_pm.adapters.polymarket.rtds_adapter import RtdsChainlinkAdapter
    from tyrex_pm.engine.dispatcher import EventDispatcher

    seen: list[tuple] = []
    holder: dict = {}

    class _Ws:
        def __init__(self) -> None:
            self.frames = ["PONG", "not json at all"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def send(self, _msg):
            return None

        async def recv(self):
            if self.frames:
                return self.frames.pop(0)
            await holder["adapter"].stop()
            raise ConnectionError("closed by test")

        async def close(self):
            return None

    adapter = RtdsChainlinkAdapter(connect=lambda *_a, **_k: _Ws(),
                                   raw_sink=lambda raw, conn: seen.append((raw, conn)))
    holder["adapter"] = adapter

    async def run():
        await asyncio.wait_for(adapter.run(EventDispatcher()), timeout=5)

    asyncio.run(run())
    assert seen == [("PONG", 1), ("not json at all", 1)]


def test_raw_stream_at_essential_level_and_fault_detection(tmp_path: Path) -> None:
    config = RecordingConfig(level=RecordingLevel.ESSENTIAL, raw_inputs=True,
                             queue_max_records=2)
    recording = open_decision_recording(config, output_directory=tmp_path, mode="live",
                                        run_id="r", schema=None, entry_band=None,
                                        manifest={})
    assert recording is not None and recording.raw is not None
    assert not recording.recorder.has_stream("decisions")
    recording.on_evaluated(SimpleNamespace(), {"at_ns": 1}, window_id="w")  # no-op
    recording.raw.message("reference_twap", "a")
    assert recording.raw_fault() is None
    for frame in ("b", "c", "d", "e"):  # beyond the queue bound before the writer drains
        recording.raw.message("reference_twap", frame)
    assert recording.raw_fault().startswith("records_dropped")
    status = recording.close()
    completeness = status["raw_capture"]["completeness"]
    assert completeness["inputs_captured"] is False
    assert (tmp_path / "recording" / "raw_completeness.json").exists()
    rows = list(iter_records(tmp_path / "recording", "raw"))
    assert rows and rows[0]["body_text"] == "a"


def test_stop_entries_blocks_only_new_entries() -> None:
    from tyrex_pm.runtime.capabilities import CapabilityController
    from tyrex_pm.runtime.trading_runtime import TradingRuntime

    events: list[tuple] = []
    recording = SimpleNamespace(raw=SimpleNamespace(fault=None),
                                raw_fault=lambda: "writer_failed: disk full",
                                stop_raw=lambda reason: events.append(("stopped", reason)))
    fake = SimpleNamespace(
        decision_recording=recording, _raw_fault_handled=False,
        config=SimpleNamespace(recording=RecordingConfig(raw_on_fault="stop_entries")),
        capabilities=CapabilityController(),
        run_recorder=SimpleNamespace(record=lambda kind, payload: events.append((kind,
                                                                                 payload))),
        _record_readiness_if_changed=lambda: None,
    )
    TradingRuntime._check_raw_capture(fake)
    TradingRuntime._check_raw_capture(fake)  # handled once
    assert "RAW_CAPTURE_FAILED" in fake.capabilities.extra_blockers
    kinds = [e[0] for e in events]
    assert kinds == ["stopped", "RAW_CAPTURE_FAILED"]
    assert events[1][1]["policy"] == "stop_entries"
    # Exits do not depend on extra blockers.
    caps = fake.capabilities
    caps.live_requested = caps.account_reads_ready = caps.active_market_ready = True
    caps.selected_token_sellable = True
    assert caps.snapshot(exposed=True).exit_executable
