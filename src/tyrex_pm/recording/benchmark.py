"""Recording benchmark: measure before setting limits (integration plan 0.6, decision 7).

Measures, for realistic decision and explain records (synthetic, or the decision rows of
a saved replay bundle):

* capture cost on the calling (trading) thread: p50/p99/max microseconds per evaluation;
* writer CPU (thread time) and throughput (records/s written, time to drain);
* peak traced memory of queued records (``tracemalloc``, separate pass: it slows capture);
* drops when the burst exceeds the queue bound;
* a slow disk (each segment write delayed) and a full disk (writes fail): the capture
  cost must stay flat, drops must be counted, the failure must be visible in status.

Nothing here touches a venue or a run; it writes only under ``out``.
"""

from __future__ import annotations

import json
import shutil
import statistics
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from tyrex_pm.recording import recorder as recorder_module
from tyrex_pm.recording.config import RecordingConfig
from tyrex_pm.recording.recorder import Recorder


def synthetic_decision(i: int) -> dict:
    from tyrex_pm.strategies.settlement_value.record_schema import DECISION_RECORD_SCHEMA

    row: dict[str, Any] = {}
    for f in DECISION_RECORD_SCHEMA.fields:
        if f.kind == "number":
            row[f.name] = 0.123456 + i * 1e-6
        elif f.kind == "bool":
            row[f.name] = bool(i % 2)
        elif f.kind == "json":
            row[f.name] = ["EDGE_BELOW_COST"]
        else:
            row[f.name] = "EDGE_BELOW_COST"
    leg = {"source_ts": "2026-10-19T12:00:00.123456+00:00",
           "receive_ts": "2026-10-19T12:00:00.140000+00:00",
           "apply_ts": "2026-10-19T12:00:00.141000+00:00", "data_age_ms": 18.0,
           "sync_health": "HEALTHY", "book_version": 1000 + i}
    fresh = {"fresh": True, "age_ms": 40, "threshold_ms": 5000, "basis": "EVENT_TIME",
             "reason": "FRESH"}
    row.update({
        "schema": "settlement_value_decision_v2", "mode": "benchmark", "run_id": "bench",
        "window_id": "btc-updown-5m-1792266900", "at_ns": 1_792_266_900_000_000_000 + i,
        "action": "WAIT", "reason": "EDGE_BELOW_COST",
        "inputs": {"decision_at": "2026-10-19T12:00:00.150000+00:00",
                   "clock": {"status": "READY", "offset_ms": 1.2, "uncertainty_ms": 8,
                             "snapshot_id": "ntp:1"},
                   "up_book": fresh, "down_book": fresh, "reference": fresh,
                   "up_leg": leg, "down_leg": dict(leg), "book_connection_epoch": 3,
                   "binance_source_ts": "2026-10-19T12:00:00.100000+00:00",
                   "chainlink_source_ts": "2026-10-19T12:00:00.000000+00:00"},
    })
    return row


def synthetic_explain(i: int) -> dict:
    estimator = {"variance": 1.2e-9, "variance_units": "per_sample", "previous_variance": 1.1e-9,
                 "last_return": 2e-5, "last_dt_s": 0.21, "sigma": 3.4e-5,
                 "sample_count": 5000 + i, "effective_samples_s": 120.0,
                 "jump_guard_tripped": False, "last_sample_ts": 1_792_266_900_000 + i,
                 "last_sample_price": 97000.5, "half_life_s": 60.0, "sigma_floor": 1e-6,
                 "source": "binance_trades", "tape_age_ms": 35.0, "max_trade_age_ms": 2000}
    return {"mode": "benchmark", "run_id": "bench", "window_id": "w", "at_ns": i,
            "tau_s": 100.0, "action": "WAIT", "reason": "EDGE_BELOW_COST",
            "indicators": {"ewma_fast": estimator, "ewma_slow": dict(estimator)},
            "strategy": None}


def bundle_decisions(path: Path) -> list[dict]:
    """Decision rows of a saved replay bundle (real field values and sizes)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data.get("decisions") or [])


@contextmanager
def _disk(mode: str, delay_s: float) -> Iterator[None]:
    original = recorder_module._Segment.write
    if mode == "normal":
        yield
        return

    def slow(self, line, capture_ns):  # noqa: ANN001
        time.sleep(delay_s)
        original(self, line, capture_ns)

    def full(self, line, capture_ns):  # noqa: ANN001
        raise OSError(28, "No space left on device (simulated)")

    recorder_module._Segment.write = slow if mode == "slow" else full
    try:
        yield
    finally:
        recorder_module._Segment.write = original


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)


def _one(out: Path, name: str, records: list[dict], explains: list[dict], *,
         config: RecordingConfig, disk: str, delay_s: float, pace_s: float,
         trace_memory: bool) -> dict:
    directory = out / name
    shutil.rmtree(directory, ignore_errors=True)
    rec = Recorder(directory, config, streams={"decisions": "bench", "explain": "bench"},
                   manifest={"benchmark": name})
    costs: list[float] = []
    with _disk(disk, delay_s):
        rec.start()
        if trace_memory:
            tracemalloc.start()
        cpu0, wall0 = time.process_time(), time.perf_counter()
        for i, record in enumerate(records):
            started = time.perf_counter_ns()
            rec.capture("decisions", dict(record))
            if explains:
                rec.capture("explain", dict(explains[i % len(explains)]))
            costs.append((time.perf_counter_ns() - started) / 1000.0)
            if pace_s:
                time.sleep(pace_s)
        capture_wall = time.perf_counter() - wall0
        peak = None
        if trace_memory:
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        status = rec.close(timeout_s=config.shutdown_timeout_s)
        total_wall = time.perf_counter() - wall0
        cpu = time.process_time() - cpu0
    streams = status["streams"]
    written = sum(s["written"] for s in streams.values())
    size = sum(p.stat().st_size for p in directory.rglob("*.zst"))
    return {
        "case": name, "disk": disk, "records": len(records),
        "explain_per_record": bool(explains),
        "capture_cost_us": {"p50": _pct(costs, 0.5), "p99": _pct(costs, 0.99),
                            "max": round(max(costs), 2) if costs else None,
                            "mean": round(statistics.fmean(costs), 2) if costs else None},
        "capture_wall_s": round(capture_wall, 4),
        "drain_wall_s": round(total_wall - capture_wall, 4),
        "writer_cpu_s": status["writer_cpu_s"],
        "process_cpu_s": round(cpu, 4),
        "written": written, "dropped": status["dropped"],
        "written_per_s": round(written / total_wall, 1) if total_wall else None,
        "failed": status["failed"],
        "errors": {n: s.get("last_error") for n, s in streams.items() if s.get("last_error")},
        "compressed_bytes": size,
        "compressed_bytes_per_decision": round(size / max(1, written), 1),
        "peak_traced_memory_mib": None if peak is None else round(peak / 2**20, 2),
    }


def run_benchmark(*, out: Path | None = None, records: int = 20_000,
                  bundle: Path | None = None, with_explain: bool = True,
                  slow_disk_delay_s: float = 0.002, pace_s: float = 0.0,
                  queue_max_records: int = 50_000) -> dict:
    out = Path(out or tempfile.mkdtemp(prefix="tyrex_recording_bench_"))
    out.mkdir(parents=True, exist_ok=True)
    base = bundle_decisions(bundle) if bundle else []
    rows = ([base[i % len(base)] for i in range(records)] if base
            else [synthetic_decision(i) for i in range(records)])
    explains = [synthetic_explain(i) for i in range(64)] if with_explain else []
    config = RecordingConfig(queue_max_records=queue_max_records, shutdown_timeout_s=30.0)
    small = RecordingConfig(queue_max_records=max(10, records // 20), shutdown_timeout_s=2.0)
    cases = [
        _one(out, "normal", rows, explains, config=config, disk="normal", delay_s=0,
             pace_s=pace_s, trace_memory=False),
        _one(out, "memory", rows, explains, config=config, disk="normal", delay_s=0,
             pace_s=pace_s, trace_memory=True),
        _one(out, "slow_disk", rows, explains, config=small, disk="slow",
             delay_s=slow_disk_delay_s, pace_s=pace_s, trace_memory=False),
        _one(out, "full_disk", rows, explains, config=config, disk="full", delay_s=0,
             pace_s=pace_s, trace_memory=False),
    ]
    result = {"source": str(bundle) if bundle else "synthetic", "out": str(out),
              "records": records, "with_explain": with_explain,
              "decision_json_bytes": len(json.dumps(rows[0], default=str)),
              "explain_json_bytes": len(json.dumps(explains[0])) if explains else 0,
              "cases": cases}
    (out / "benchmark.json").write_text(json.dumps(result, indent=1, default=str),
                                        encoding="utf-8")
    return result
