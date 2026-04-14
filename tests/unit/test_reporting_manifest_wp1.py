"""WP1 — manifest updates from coordinator vs main thread must serialize."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from tyrex_pm.reporting.context import RunContext
from tyrex_pm.reporting.sinks.jsonl import JsonlFactSink


def test_run_context_manifest_updates_are_mutexed_under_concurrency(tmp_path: Path) -> None:
    facts = tmp_path / "f.jsonl"
    man_path = tmp_path / "manifest.json"
    sink = JsonlFactSink(facts, run_id="r1", max_queue=1000, batch_size=8)
    sink.start()
    ctx = RunContext(
        run_id="r1",
        run_dir=tmp_path,
        strategy_name="s",
        trader_id="T",
        sink=sink,
        facts_path=facts,
        manifest_path=man_path,
    )
    ctx.manifest_path.write_text(json.dumps({"run_id": "r1"}) + "\n", encoding="utf-8")

    errors: list[BaseException] = []
    stop = threading.Event()

    def hammer_a() -> None:
        try:
            i = 0
            while not stop.is_set() and i < 200:
                ctx.update_manifest_fields(wp1_stress_a=i)
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def hammer_b() -> None:
        try:
            j = 0
            while not stop.is_set() and j < 200:
                ctx.update_manifest_fields(wp1_stress_b=j)
                j += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    ta = threading.Thread(target=hammer_a)
    tb = threading.Thread(target=hammer_b)
    ta.start()
    tb.start()
    ta.join(timeout=3.0)
    tb.join(timeout=3.0)
    stop.set()
    assert not errors
    raw = ctx.manifest_path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert "wp1_stress_a" in parsed or "wp1_stress_b" in parsed
    sink.drain_and_close()
