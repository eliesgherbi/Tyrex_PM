"""Synthetic recorder/normalizer load proof. No network, credentials, or trading imports."""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from tyrex_pm.data_warehouse.capture.files import atomic_json, read_json
from tyrex_pm.data_warehouse.capture.writer import RawWriter, raw_manifests
from tyrex_pm.data_warehouse.config import Profile, Storage
from tyrex_pm.data_warehouse.contracts import canonical
from tyrex_pm.data_warehouse.normalize.worker import normalize_once


def resident_bytes(pid: int | None = None) -> int | None:
    if os.name != "nt":
        if pid is not None:
            return None
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "PeakWorkingSetSize",
                "WorkingSetSize",
                "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage",
                "PagefileUsage",
                "PeakPagefileUsage",
            )
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(Counters),
        wintypes.DWORD,
    ]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid) if pid else kernel.GetCurrentProcess()
    if not handle:
        return None
    try:
        success = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    finally:
        if pid:
            kernel.CloseHandle(handle)
    return counters.WorkingSetSize if success else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--rate", type=int, default=1500)
    args = parser.parse_args()
    root = args.root.resolve()
    if root.exists() or args.seconds <= 0 or args.rate <= 0:
        parser.error("new root and positive seconds/rate required")
    profile = replace(Profile(), storage=replace(Storage(), raw_rotate_s=10, min_free_disk_gib=0))
    atomic_json(
        root / "collections/benchmark/collection.json",
        {"profile": asdict(profile), "profile_hash": profile.hash, "synthetic": True},
    )
    writer = RawWriter(root, "benchmark", "load-session", profile.hash, profile.storage)
    stop = root / "normalizer.stop"
    log = (root / "normalizer.log").open("wb")
    normalizer = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tyrex_pm.data_warehouse.cli",
            "normalize",
            "--root",
            str(root),
            "--collection",
            "benchmark",
            "--follow",
            "--stop-file",
            str(stop),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    start = time.perf_counter()
    last_cpu, last_sample_at = time.process_time(), start
    count = 0
    samples = []
    enqueue_histogram = {
        str(v): 0 for v in (10000, 50000, 100000, 250000, 500000, 1000000, 2000000, 10000000)
    }
    enqueue_histogram["above_10ms"] = 0
    max_enqueue = 0
    failure = None
    try:
        while time.perf_counter() - start < args.seconds:
            target = int((time.perf_counter() - start) * args.rate)
            while count < target:
                payload = {
                    "e": "trade",
                    "t": count,
                    "p": "60000.12345678",
                    "q": "0.00012500",
                    "m": bool(count % 2),
                    "T": time.time_ns() // 1000000,
                }
                before = time.perf_counter_ns()
                writer.submit(canonical(payload), source="binance", stream="synthetic-trades")
                elapsed = time.perf_counter_ns() - before
                max_enqueue = max(max_enqueue, elapsed)
                bucket = next(
                    (
                        str(v)
                        for v in (10000, 50000, 100000, 250000, 500000, 1000000, 2000000, 10000000)
                        if elapsed <= v
                    ),
                    "above_10ms",
                )
                enqueue_histogram[bucket] += 1
                count += 1
            if len(samples) <= int((time.perf_counter() - start) // 5):
                current_cpu, current_at = time.process_time(), time.perf_counter()
                state_file = root / "normalization_status.json"
                normalized = read_json(state_file) if state_file.exists() else {}
                latest = normalized.get("last_received_at")
                sample = {
                    "elapsed_s": time.perf_counter() - start,
                    "records": count,
                    "resident_bytes": resident_bytes(),
                    "normalizer_resident_bytes": resident_bytes(normalizer.pid),
                    "writer_cpu_percent_one_core": 100
                    * (current_cpu - last_cpu)
                    / max(current_at - last_sample_at, 1e-6),
                    "normalization_lag_s": max(0, (time.time_ns() - latest) / 10**9)
                    if latest
                    else None,
                    **writer.status(),
                }
                last_cpu, last_sample_at = current_cpu, current_at
                samples.append(sample)
                atomic_json(root / "progress.json", sample)
            if normalizer.poll() is not None:
                raise RuntimeError("normalizer exited unexpectedly")
            time.sleep(0.001)
    except BaseException as exc:
        failure = type(exc).__name__
    finally:
        writer.close(60)
        stop.touch()
        try:
            normalizer.wait(60)
        except subprocess.TimeoutExpired:
            normalizer.terminate()
            normalizer.wait(10)
            failure = failure or "normalizer_shutdown_timeout"
        log.close()
    normalization = normalize_once(root, "benchmark")
    manifests = raw_manifests(root, "benchmark")
    report = {
        "synthetic": True,
        "network": False,
        "duration_s": time.perf_counter() - start,
        "target_rate": args.rate,
        "records": count,
        "failure": failure,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "measurement_scope": {
            "cpu": "writer_process_including_threads; normalizer_cpu_unmeasured",
            "memory": "current_working_set_on_windows; self_peak_rss_elsewhere",
            "traffic": "synthetic_binance_trades_only; not_mixed_live_feed_qualification",
        },
        "enqueue_histogram_upper_bounds_ns": enqueue_histogram,
        "max_enqueue_ns": max_enqueue,
        "samples": samples,
        "writer": writer.status(),
        "durable_latency_sample_scope": "last_10000_records",
        "durable_latency_p99_ns": sorted(writer.fsync_samples_ns)[
            int(len(writer.fsync_samples_ns) * 0.99)
        ]
        if writer.fsync_samples_ns
        else None,
        "raw_records": sum(m["record_count"] for m in manifests),
        "raw_compressed_bytes": sum(m["compressed_bytes"] for m in manifests),
        "raw_uncompressed_bytes": sum(m["uncompressed_bytes"] for m in manifests),
        "final_normalization": normalization,
    }
    atomic_json(root / "benchmark_report.json", report)
    print(canonical({k: v for k, v in report.items() if k != "samples"}))
    return 0 if not failure and report["raw_records"] == count else 1


if __name__ == "__main__":
    raise SystemExit(main())
