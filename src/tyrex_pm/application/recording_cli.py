"""``tyrex-pm recording``: prune old decision/explain/raw recordings; benchmark capture.

Prune is a dry run unless ``--apply``. It only removes ``recording/`` folders written by
the recorder (identified by their ``manifest.json``); run summaries, journals, ledgers and
the execution database are never touched.
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from tyrex_pm.recording.recorder import RECORDER_VERSION


def add_parser(commands: Any) -> None:
    rec = commands.add_parser("recording", help="Decision recording maintenance and benchmark")
    sub = rec.add_subparsers(dest="recording_command", required=True)
    prune = sub.add_parser("prune", help="List (or with --apply delete) old recordings")
    prune.add_argument("--root", type=Path, required=True,
                       help="folder holding run outputs (searched recursively)")
    prune.add_argument("--older-than-days", type=float, default=30.0)
    prune.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    bench = sub.add_parser("benchmark", help="Measure capture cost, writer CPU, memory, disk")
    bench.add_argument("--out", type=Path, default=None)
    bench.add_argument("--records", type=int, default=20_000)
    bench.add_argument("--bundle", type=Path, default=None,
                       help="saved replay result JSON: use its real decision rows")
    bench.add_argument("--no-explain", action="store_true")
    bench.add_argument("--slow-disk-ms", type=float, default=2.0)
    bench.add_argument("--pace-ms", type=float, default=0.0,
                       help="sleep between evaluations (0 = burst)")
    rec = sub.add_parser(
        "reconstruct",
        help="Import a raw-captured observation into its own warehouse collection and "
        "reconstruct its decisions at the captured evaluation instants (offline)")
    rec.add_argument("--capture", type=Path, required=True,
                     help="observation output folder (decisions.jsonl + recording/raw)")
    rec.add_argument("--out", type=Path, default=None,
                     help="report folder (default: <capture>/reconstruction)")


def recordings_to_prune(root: Path, older_than_days: float, *, now: float | None = None
                        ) -> list[tuple[Path, float, int]]:
    """(folder, age_days, bytes) of recorder folders older than the limit."""
    now = time.time() if now is None else now
    found = []
    for manifest in sorted(Path(root).rglob("recording/manifest.json")):
        folder = manifest.parent
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("recorder") != RECORDER_VERSION:
            continue
        status = folder / "status.json"
        newest = max(p.stat().st_mtime for p in (manifest, status) if p.exists())
        age_days = (now - newest) / 86400.0
        if age_days >= older_than_days:
            size = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
            found.append((folder, age_days, size))
    return found


def run(args: argparse.Namespace) -> int:
    if args.recording_command == "prune":
        found = recordings_to_prune(args.root, args.older_than_days)
        total = sum(size for _, _, size in found)
        for folder, age, size in found:
            print(f"{'DELETE' if args.apply else 'would delete'} {folder} "
                  f"({age:.1f} days, {size / 2**20:.1f} MiB)")
            if args.apply:
                shutil.rmtree(folder)
        print(f"{len(found)} recording(s), {total / 2**20:.1f} MiB"
              + ("" if args.apply else " - dry run, pass --apply to delete"))
        return 0
    if args.recording_command == "reconstruct":
        from tyrex_pm.research.strategy_replay.capture_replay import reconstruct

        report = reconstruct(args.capture, out=args.out or args.capture / "reconstruction")
        summary = {
            "decisions_reconstructed_equal": report["decisions_reconstructed_equal"],
            "raw_completeness": report["raw_completeness"],
            "windows": [{"window_id": w["window_id"], **{k: (w.get("comparison") or {}).get(k)
                         for k in ("live_evaluations", "replayed_evaluations",
                                   "decision_equal", "fully_equal",
                                   "field_mismatch_counts")}} for w in report["windows"]],
        }
        print(json.dumps(summary, indent=1, default=str))
        return 0
    from tyrex_pm.recording.benchmark import run_benchmark

    result = run_benchmark(out=args.out, records=args.records, bundle=args.bundle,
                           with_explain=not args.no_explain,
                           slow_disk_delay_s=args.slow_disk_ms / 1000.0,
                           pace_s=args.pace_ms / 1000.0)
    print(json.dumps(result, indent=1, default=str))
    return 0
