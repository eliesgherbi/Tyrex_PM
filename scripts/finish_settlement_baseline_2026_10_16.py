"""Finish the frozen v2 baseline's B/C stages without running notebook A again."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from tyrex_pm.research.strategy_lab import staged as st  # noqa: E402
from tyrex_pm.research.strategy_replay.run import RUNNER_VERSION  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("B", "C"))
    parser.add_argument("--finalist", action="append", default=[])
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    exp = st.open_experiment(REPO / "var/research/strategy_runs/staged/full_development-7f132d3ee2")
    anchor = exp.stage_record("A")
    if not anchor or anchor["status"] != "completed":
        raise RuntimeError("The baseline stage A must already be complete.")
    if RUNNER_VERSION != "strategy_closed_loop_v2":
        raise RuntimeError("This baseline must use replay v2, without new features.")
    before = st.code_identity()["source_fingerprint"]
    if before != anchor["code"]["source_fingerprint"]:
        raise RuntimeError("Current source differs from stage A; refusing to mix code.")
    for previous in ("B", "C"):
        record = exp.stage_record(previous)
        if record and record["code"]["source_fingerprint"] != before:
            raise RuntimeError(f"Stage {previous} used different source.")
    print(
        json.dumps(
            {
                "experiment": exp.id,
                "stage": args.stage,
                "runner": RUNNER_VERSION,
                "windows": len(exp.windows),
                "source_fingerprint": before,
                "finalists": args.finalist,
                "keep_timelines": True,
                "workers": args.workers,
            }
        ),
        flush=True,
    )
    print(
        st.preview(
            exp,
            args.stage,
            workers=args.workers,
            finalists=args.finalist,
            keep_timelines=True,
        ).to_string(),
        flush=True,
    )
    results = st.run_stage(
        exp,
        args.stage,
        workers=args.workers,
        finalists=args.finalist,
        keep_timelines=True,
    )
    after = st.code_identity()["source_fingerprint"]
    if after != before:
        raise RuntimeError("Source changed during execution; review saved results before use.")
    print(f"Completed stage {args.stage}: {len(results)} rows saved.", flush=True)


if __name__ == "__main__":
    main()
