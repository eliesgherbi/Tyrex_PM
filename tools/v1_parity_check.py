"""V1 parity gate: re-run saved workspace replay jobs and compare with their stored results.

Each unit of a job is replayed again through the workspace worker on the same pinned data,
parameters and scenario file, and the new decision digest and economics are compared with
the part saved when the job ran. Any difference is printed; the exit code is the number of
differing units (0 = parity).

    python tools/v1_parity_check.py 4b70f7da6cea454da1acc5451e3d36d1 7fb99d52682c41df92065325ca92c235
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOBS = ROOT / "var" / "research" / "workflows" / "ihm" / "jobs"
COMPARED = ("decision_digest", "entered", "filled", "realized_pnl", "qualification", "k_value")


def _saved_parts(job: Path) -> dict[str, dict]:
    parts = {}
    for path in (job / "parts").glob("*.json"):
        if path.name == "manifest.json" or path.name.endswith(".decisions.json"):
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        parts[body["unit_id"]] = body
    return parts


def _replay(unit: dict) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from tyrex_pm.research.workflows.worker import _strategy_replay

    return _strategy_replay(unit)


def check(job_id: str, workers: int, limit: int = 0) -> int:
    from tyrex_pm.research.workflows.worker import _with_manifests

    job = JOBS / job_id
    body = json.loads((job / "job.json").read_text(encoding="utf-8"))
    saved = _saved_parts(job)
    units = [_with_manifests(u, body.get("pins")) for u in body["units"] if u["id"] in saved]
    if limit:
        units = units[:limit]
    differing = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for unit, fresh in zip(units, pool.map(_replay, units)):
            old = saved[unit["id"]]
            diff = {k: (old.get(k), fresh.get(k)) for k in COMPARED if old.get(k) != fresh.get(k)}
            if diff:
                differing += 1
                print(f"DIFF {unit['id']}: {diff}")
    print(f"{job_id}: {len(units)} units replayed, {differing} differ")
    return differing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("jobs", nargs="+")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, default=0, help="replay only the first N units")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    return sum(check(job, args.workers, args.limit) for job in args.jobs)


if __name__ == "__main__":
    raise SystemExit(main())
